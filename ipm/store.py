"""On-disk store: JSON files under the captured plugin data dir, fcntl flock
read-modify-write, atomic replace, 0600 (BUILD_SPEC §3 architecture, §4).

Files: ``ledger.json`` (per-hashed-session provenance), ``confirmations.json``
(route confirmations), ``meta.json`` (generations, consent, sweep bookkeeping).
Keys are ``s:<sha256(session_id)[:24]>`` — never raw session ids, never content.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("ipm.store")

FILES = ("ledger", "confirmations", "meta")
_lock_lock = threading.Lock()
_lock_handles: dict[str, int] = {}


def hash_session_id(session_id: str) -> str:
    """Content-free key for a raw session id (§2.5: hashed ids as keys)."""
    return "s:" + hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]


def validate_session_id(session_id: str) -> bool:
    """Exact-id operations only (§4): ``^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$``."""
    if not isinstance(session_id, str) or not session_id:
        return False
    import re

    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", session_id) is not None


def _path(data_dir: Path, kind: str) -> Path:
    if kind not in FILES:
        raise ValueError(f"unknown store file {kind!r}")
    return Path(data_dir) / f"{kind}.json"


def _lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / "store.lock"


# Reentrancy registry: {(thread ident, data_dir path): depth}. flock is NOT
# reentrant within a process (a second open+flock self-deadlocks), which is
# exactly why the delete fence could not hold the lock across the destructive
# call: core fires our own session-delete hook synchronously inside
# delete_session. Per-thread, per-dir reentrancy lets OUR hooks nest while
# other threads/processes still serialize on the flock.
_reentrancy_lock = threading.Lock()
_registry_pid = os.getpid()
# key: (pid, thread ident, lock identity) — round-#4 Astra F4: a forked child
# inherits the parent's registry and thread idents, which would let it bypass
# the flock entirely; store_lock() resets the registry when the pid changes.
_held_locks: dict[tuple[int, int, str], int] = {}


@contextmanager
def store_lock(data_dir: Path, blocking: bool = True):
    """Cross-process RMW lock keyed by the captured data dir (profile-safe, §2.6).

    Reentrant within one thread for the SAME data dir (our hooks fire inside
    core callbacks that run while we hold the lock); cross-thread and
    cross-process callers still serialize on the flock, so a concurrent
    keep/off landing can never interleave with a fence that holds the lock.
    """
    data_dir = Path(data_dir)
    global _registry_pid
    with _reentrancy_lock:
        if _registry_pid != os.getpid():
            # forked child: inherited depth is not ours (round-#4 Astra F4).
            # The child is single-threaded right after fork, so replacing the
            # registry contents is race-free; inherited flock fds are shared
            # open-file descriptions and are dropped when the parent closes.
            _held_locks.clear()
            _registry_pid = os.getpid()
    token = (os.getpid(), threading.get_ident(), str(data_dir))
    with _reentrancy_lock:
        depth = _held_locks.get(token, 0)
    # NEVER hold _reentrancy_lock across the yield: the caller's body (and any
    # nested store_lock from our own hooks) runs while this generator is
    # suspended — holding it here would self-deadlock the nesting it enables.
    if depth > 0:
        with _reentrancy_lock:
            _held_locks[token] = depth + 1
        try:
            yield True
        finally:
            with _reentrancy_lock:
                left = _held_locks[token] - 1
                if left <= 0:
                    _held_locks.pop(token, None)
                else:
                    _held_locks[token] = left
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    fd = os.open(_lock_path(data_dir), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        mode = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, mode)
        except BlockingIOError:
            yield False
            return
        with _reentrancy_lock:
            _held_locks[token] = _held_locks.get(token, 0) + 1
        try:
            yield True
        finally:
            with _reentrancy_lock:
                left = _held_locks.get(token, 1) - 1
                if left <= 0:
                    _held_locks.pop(token, None)
                else:
                    _held_locks[token] = left
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def audit_lock(data_dir: Path, blocking: bool = True):
    """Dedicated flock for audit.jsonl appends/rotation (round-#4 Astra F7):
    never held by the deletion fence, so optional audit logging cannot
    serialize a foreground request behind a long fence hold. Same
    reentrancy discipline as store_lock."""
    audit_dir = Path(data_dir)
    token = (os.getpid(), threading.get_ident(), str(audit_dir) + "::audit")
    global _registry_pid
    with _reentrancy_lock:
        if _registry_pid != os.getpid():
            _held_locks.clear()
            _registry_pid = os.getpid()
    with _reentrancy_lock:
        depth = _held_locks.get(token, 0)
    if depth > 0:
        with _reentrancy_lock:
            _held_locks[token] = depth + 1
        try:
            yield True
        finally:
            with _reentrancy_lock:
                left = _held_locks[token] - 1
                if left <= 0:
                    _held_locks.pop(token, None)
                else:
                    _held_locks[token] = left
        return
    audit_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(audit_dir, 0o700)
    fd = os.open(audit_dir / "audit.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        mode = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, mode)
        except BlockingIOError:
            yield False
            return
        with _reentrancy_lock:
            _held_locks[token] = _held_locks.get(token, 0) + 1
        try:
            yield True
        finally:
            with _reentrancy_lock:
                left = _held_locks.get(token, 1) - 1
                if left <= 0:
                    _held_locks.pop(token, None)
                else:
                    _held_locks[token] = left
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ipm-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(kind: str, data_dir: Path) -> dict[str, Any]:
    return _read_file(_path(data_dir, kind))


def mutate(kind: str, data_dir: Path, fn: Callable[[dict[str, Any]], dict[str, Any] | None],
           blocking: bool = True) -> Any:
    """Locked read-modify-write. *fn* receives the current dict and returns the
    new dict (or None to keep it). Returns fn's return value if it returns a
    non-dict (pass-through), else None."""
    kind = kind if kind in FILES else kind
    with store_lock(data_dir, blocking=blocking) as locked:
        if not locked:
            return None
        path = _path(data_dir, kind)
        current = _read_file(path)
        result = fn(current)
        if isinstance(result, dict):
            _atomic_write(path, result)
        return result if not isinstance(result, dict) else None


# --- ledger -----------------------------------------------------------------

def ledger_get(ledger: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    return ledger.get(hash_session_id(session_id))


def lineage_hashes(ledger: dict[str, Any], root_hash: str) -> set[str]:
    """All ledger keys connected to *root_hash* through recorded parent links."""
    seen = {root_hash}
    frontier = [root_hash]
    while frontier:
        current = frontier.pop()
        entry = ledger.get(current) or {}
        for related in [entry.get("parent_hash"), *(entry.get("children") or [])]:
            if related and related in ledger and related not in seen:
                seen.add(related)
                frontier.append(related)
    return seen


# --- meta / generations ------------------------------------------------------

def current_generation(meta: dict[str, Any]) -> dict[str, Any] | None:
    generations = meta.get("generations")
    if isinstance(generations, list) and generations:
        last = generations[-1]
        if isinstance(last, dict):
            return last
    return None


def new_generation(meta: dict[str, Any], *, armed: bool, reason: str, now: float) -> dict[str, Any]:
    """Append an arming generation (mode change, consent change) — §3.3."""
    generations = meta.get("generations")
    generations = [g for g in generations if isinstance(g, dict)] if isinstance(generations, list) else []
    n = len(generations) + 1
    generations.append({"n": n, "started_at": now, "armed": bool(armed), "reason": str(reason)[:40]})
    meta["generations"] = generations
    return meta


def sweep_lock(data_dir: Path) -> Any:
    """Non-blocking daemon-sweep lock (§3.3): a contextmanager yielding True if acquired."""
    path = Path(data_dir) / "sweep.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    @contextmanager
    def _ctx():
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return _ctx()


def set_consent(meta: dict[str, Any], recorded: bool, now: float, what: str = "history_cleanup") -> dict[str, Any]:
    consent = meta.get("consent") if isinstance(meta.get("consent"), dict) else {}
    consent.update({"recorded": bool(recorded), "at": now, "what": what})
    meta["consent"] = consent
    return meta
