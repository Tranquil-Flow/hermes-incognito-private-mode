"""Provenance state machine, fenced deletion, opportunistic sweeps
(BUILD_SPEC §3.3, decisions 1A/2A).

States: candidate -> armed -> deleted, or -> excluded (permanent tombstone).
Nothing outside an ``armed`` + closed ledger lineage is ever deleted. All
deletion goes through the public fenced recipe:

    targets = SessionDB(db_path).get_session_delete_targets(id)
    delete_session(id, sessions_dir=None, expected_delete_ids=targets,
                   exclude_active_write_guards=True)

Never ``include_compression_chain=True``, never the CLI ``sessions delete``,
never ``--force``. Sweeps are opportunistic (no timers): daemon threads with
captured inputs, non-blocking flock, bounded batches (<= 20 sessions).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .settings import (
    CapturedPaths,
    cleanup_enrolled,
    effective_mode,
    platform_in_cleanup_scope,
    read_settings,
)

logger = logging.getLogger("ipm.lifecycle")

# Pinned by tests/contract/test_source_pins.py against the citing source lines.
ALLOWED_END_REASONS = {
    "cli_close",      # hermes_cli/cli_shutdown.py:289
    "new_session",    # hermes_cli/cli_session_mixin.py:594
    "resumed_other",  # hermes_cli/cli_commands_mixin.py:864 (/resume switch)
    "branched",       # hermes_cli/cli_commands_mixin.py:975 (/branch switch)
    "tui_close",      # tui_gateway/session_lifecycle.py:430 (explicit user close)
    "agent_close",    # run_agent.py:1025 (first reason wins)
}
# Rows with these reasons on desktop stay open in Hermes and are never deleted
# automatically (tui_gateway/session_lifecycle.py:60).
AUTOMATIC_END_REASONS = {"ws_orphan_reap", "ws_disconnect", "idle_timeout", "lru_evict", "tui_shutdown"}

MAX_BATCH = 20
SWEEP_MIN_INTERVAL = 10 * 60  # rate-limited llm_request trigger (§3.3)
START_SWEEP_MIN_INTERVAL = 30  # on_session_start trigger
_ID_TS_RE = re.compile(r"^\d{8}_\d{6}_\d{6}$")


# --- read-only row helpers ---------------------------------------------------

def _ro_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def read_row(db_path: Path, session_id: str) -> dict[str, Any] | None:
    if not (Path(db_path).exists() and re.match(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$", session_id)):
        return None
    try:
        conn = _ro_connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, started_at, ended_at, end_reason, source, parent_session_id "
                "FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def min_message_ts(db_path: Path, session_id: str) -> float | None:
    try:
        conn = _ro_connect(db_path)
        try:
            row = conn.execute(
                "SELECT MIN(timestamp) AS t FROM messages WHERE session_id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None or row["t"] is None:
        return None
    return float(row["t"])


def row_is_closed(row: dict[str, Any]) -> bool:
    """Closed = ended_at set AND end_reason in the pinned allowlist (§3.3)."""
    if row is None or row.get("ended_at") is None:
        return False
    return row.get("end_reason") in ALLOWED_END_REASONS


# --- ledger mutations --------------------------------------------------------

def _entry(ledger: dict[str, Any], session_id: str, platform: str | None, generation: int | None,
           parent_hash: str | None, now: float) -> dict[str, Any]:
    from .store import hash_session_id

    return {
        "state": "candidate",
        "raw_id": str(session_id),  # identifier (not content); needed for exact-id row reads
        "generation": generation,
        "platform": platform or "",
        "parent_hash": parent_hash,
        "created_ts": now,
        "children": [],
        "attempts": 0,
    }


def observe_session_start(session_id: str | None, platform: str | None, paths: CapturedPaths) -> None:
    """on_session_start: create a candidate when cleanup could ever apply (§3.3)."""
    from . import store

    if not session_id or not store.validate_session_id(str(session_id)):
        return
    settings = read_settings(paths)
    meta = store.read("meta", paths.data_dir)
    gen = store.current_generation(meta)
    generation = int(gen["n"]) if gen else None
    enrolled = cleanup_enrolled(settings, meta)
    platform = str(platform or "")

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        key = store.hash_session_id(str(session_id))
        existing = ledger.get(key)
        if existing is not None:
            return ledger
        if not enrolled or not platform_in_cleanup_scope(platform, settings) \
                or settings.get("keep_history") or generation is None:
            ledger[key] = {
                "state": "excluded",
                "excluded_reason": (
                    "cleanup not enrolled" if not enrolled
                    else "platform out of cleanup scope" if not platform_in_cleanup_scope(platform, settings)
                    else "keep_history=true at creation"
                ),
                "platform": platform,
                "first_seen": time.time(),
            }
            return ledger
        ledger[key] = _entry(ledger, str(session_id), platform, generation, None, time.time())
        return ledger

    store.mutate("ledger", paths.data_dir, fn)
    maybe_sweep(paths, trigger="session_start")


def observe_subagent_start(parent_session_id: str | None, child_session_id: str | None,
                           paths: CapturedPaths) -> None:
    """subagent_start: delegate children join the parent's lineage state (§3.3)."""
    from . import store

    if not parent_session_id or not child_session_id:
        return
    if not (store.validate_session_id(str(parent_session_id)) and store.validate_session_id(str(child_session_id))):
        return
    parent_hash = store.hash_session_id(str(parent_session_id))
    child_hash = store.hash_session_id(str(child_session_id))

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        changed = False
        parent = ledger.get(parent_hash)
        child = ledger.get(child_hash)
        if parent is not None and child is None:
            child = dict(parent)
            child.update({"parent_hash": parent_hash, "created_ts": time.time(),
                          "raw_id": str(child_session_id),
                          "state": parent.get("state") if parent.get("state") == "excluded" else "candidate",
                          "children": [], "attempts": 0})
            if child["state"] != "excluded":
                child.pop("excluded_reason", None)
            ledger[child_hash] = child
            parent.setdefault("children", [])
            if child_hash not in parent["children"]:
                parent["children"].append(child_hash)
            changed = True
        elif child is not None and parent is None:
            # Parent never seen: unknown provenance -> the child is protected.
            ledger[child_hash] = {
                "state": "excluded",
                "excluded_reason": "parent provenance unknown",
                "parent_hash": parent_hash,
                "first_seen": time.time(),
            }
            changed = True
        if changed:
            for entry in (ledger.get(parent_hash), ledger.get(child_hash)):
                if entry is not None and entry.get("override") is None:
                    entry.pop("override", None)
        return ledger

    store.mutate("ledger", paths.data_dir, fn)


def observe_post_compaction(session_id: str | None, old_session_id: str | None,
                            paths: CapturedPaths) -> None:
    """post_compaction: link the continuation to its predecessor (§3.1/§3.3)."""
    from . import store

    if not session_id or not old_session_id:
        return
    if not (store.validate_session_id(str(session_id)) and store.validate_session_id(str(old_session_id))):
        return
    new_hash = store.hash_session_id(str(session_id))
    old_hash = store.hash_session_id(str(old_session_id))

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        old = ledger.get(old_hash)
        if old is None:
            return ledger
        entry = ledger.get(new_hash)
        if entry is None:
            entry = dict(old)
            entry.update({"parent_hash": old_hash, "created_ts": time.time(),
                          "raw_id": str(session_id), "children": [], "attempts": 0})
            if entry["state"] != "excluded":
                entry["state"] = "candidate"
            ledger[new_hash] = entry
        else:
            entry["parent_hash"] = old_hash
        old.setdefault("children", [])
        if new_hash not in old["children"]:
            old["children"].append(new_hash)
        return ledger

    store.mutate("ledger", paths.data_dir, fn)


def set_override(session_id: str, override: str, paths: CapturedPaths) -> bool:
    """/private keep|off|on: override this chat; keep/off exclude the lineage (§3.3)."""
    from . import store

    if override not in {"on", "off", "keep"}:
        return False
    if not store.validate_session_id(session_id):
        return False
    key = store.hash_session_id(session_id)

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        entry = ledger.get(key)
        if entry is None:
            entry = {"state": "excluded" if override in {"off", "keep"} else "candidate",
                     "raw_id": str(session_id),
                     "first_seen": time.time(), "platform": "",
                     "excluded_reason": f"/private {override}" if override in {"off", "keep"} else None,
                     "generation": None, "parent_hash": None, "children": [], "attempts": 0}
            if override == "on":
                entry.pop("excluded_reason", None)
            ledger[key] = entry
        entry["override"] = override
        if override in {"off", "keep"}:
            for h in store.lineage_hashes(ledger, key):
                member = ledger.get(h)
                if member is not None and member.get("state") != "deleted":
                    member["state"] = "excluded"
                    member["excluded_reason"] = f"lineage /private {override}"
                    member["override"] = override
        return ledger

    store.mutate("ledger", paths.data_dir, fn)
    return True


def on_external_delete(session_id: str, reason: str, paths: CapturedPaths) -> None:
    """on_session_delete: invalidation hint ONLY (§4) — fires for archive too.
    Verify row state before marking (exact-id absence = externally removed)."""
    from . import store

    if not session_id or not store.validate_session_id(str(session_id)):
        return
    key = store.hash_session_id(str(session_id))
    row = read_row(paths.db_path, str(session_id))

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        entry = ledger.get(key)
        if entry is None:
            return ledger
        if row is None:
            # Row really gone (delete/prune/repair or our own fenced delete).
            if entry.get("state") in {"armed", "candidate"}:
                entry["state"] = "deleted"
                entry["deleted_how"] = "external"
                for h in store.lineage_hashes(ledger, key):
                    member = ledger.get(h)
                    if member is not None and member.get("state") in {"armed", "candidate"}:
                        member["state"] = "deleted"
                        member["deleted_how"] = "external"
        elif reason == "archive" and entry.get("state") in {"armed", "candidate"}:
            entry["state"] = "excluded"
            entry["excluded_reason"] = "archived by user"
        return ledger

    store.mutate("ledger", paths.data_dir, fn)


# --- validation / arming -----------------------------------------------------

def _validate_for_arming(row: dict[str, Any], entry: dict[str, Any], gen: dict[str, Any] | None,
                         db_path: Path) -> tuple[bool, str]:
    """candidate -> armed checks (§3.3): row exists, whole life inside one armed
    generation, no message older than the generation start."""
    if gen is None:
        return False, "no arming generation"
    if not gen.get("armed"):
        return False, "current generation disarmed"
    if entry.get("generation") != gen.get("n"):
        return False, "created outside the current generation"
    started = row.get("started_at")
    gen_start = gen.get("started_at")
    if not isinstance(started, (int, float)) or not isinstance(gen_start, (int, float)):
        return False, "missing timestamps"
    if float(started) < float(gen_start):
        return False, "row predates the arming generation"
    mts = min_message_ts(db_path, str(row["id"]))
    if mts is not None and mts < float(gen_start):
        return False, "messages predate the arming generation"
    if entry.get("override") in {"off", "keep"}:
        return False, "user kept this chat"
    return True, ""


# --- sweep -------------------------------------------------------------------

def maybe_sweep(paths: CapturedPaths, trigger: str = "session_start", force: bool = False) -> dict[str, int]:
    """Rate-limited sweep trigger from hooks. Never inline long work: spawns a
    daemon thread with captured inputs (§2.6/§3.3)."""
    from . import store

    min_interval = SWEEP_MIN_INTERVAL if trigger == "llm_request" else START_SWEEP_MIN_INTERVAL
    if not force:
        meta = store.read("meta", paths.data_dir)
        last = meta.get("last_sweep") or {}
        if isinstance(last, dict) and (time.time() - float(last.get("at") or 0)) < min_interval:
            return {"skipped": 1}
    t = None
    from agent.memory_provider import spawn_context_thread  # public allowlist (§2.6)

    try:
        t = spawn_context_thread(_sweep_guarded, name="ipm-sweep", args=(paths,))
    except Exception:
        t = threading.Thread(target=_sweep_guarded, args=(paths,), name="ipm-sweep", daemon=True)
    t.start()
    return {"started": 1}


def _sweep_guarded(paths: CapturedPaths) -> dict[str, int]:
    try:
        return run_sweep(paths)
    except Exception:
        logger.debug("ipm: sweep failed")
        return {"error": 1}


def run_sweep(paths: CapturedPaths, batch_limit: int = MAX_BATCH) -> dict[str, int]:
    """One opportunistic sweep. Non-blocking flock; bounded batch; content-free logs."""
    from . import store

    counts: dict[str, int] = {"attempted": 0, "deleted": 0, "refused": 0, "busy": 0, "armed": 0, "dropped": 0}
    with store.sweep_lock(paths.data_dir) as acquired:
        if not acquired:
            return {"skipped_locked": 1}
        settings = read_settings(paths)
        meta = store.read("meta", paths.data_dir)
        enrolled = cleanup_enrolled(settings, meta)
        gen = store.current_generation(meta)
        ledger = store.read("ledger", paths.data_dir)
        candidates: list[str] = []
        for key, entry in ledger.items():
            if not isinstance(entry, dict):
                continue
            state = entry.get("state")
            if state == "candidate" and gen is not None and enrolled:
                ok, why = _validate_for_arming_from_ledger(entry, gen, paths)
                if ok:
                    entry["state"] = "armed"
                    counts["armed"] += 1
                else:
                    if why in {"row predates the arming generation", "messages predate the arming generation",
                               "user kept this chat"}:
                        entry["state"] = "excluded"
                        entry["excluded_reason"] = why
                        counts["dropped"] += 1
                    elif why in {"missing timestamps"} and entry.get("row_missing_at"):
                        # Candidate whose row never appeared: drop it silently.
                        counts["dropped"] += 1
                continue
            if state == "armed":
                candidates.append(key)
        if counts["armed"]:
            store.mutate("ledger", paths.data_dir, lambda l: ledger)

        for key in candidates[:batch_limit]:
            entry = ledger.get(key) or {}
            outcome = _delete_if_ready(key, entry, ledger, settings, meta, paths, enrolled)
            counts[outcome] = counts.get(outcome, 0) + 1

        store.mutate("meta", paths.data_dir, lambda m: {**m, "last_sweep": {"at": time.time(),
                     **{k: v for k, v in counts.items()}}})
    return counts


def _validate_for_arming_from_ledger(entry: dict[str, Any], gen: dict[str, Any], paths: CapturedPaths) -> tuple[bool, str]:
    """Sweep-time validation for a candidate whose raw id we must recover.

    The ledger is keyed by hash; to read the row we need the raw id, so candidates
    store a ``raw_id`` field (ids are identifiers, not content — §2.5 allows
    provider/session ids as verdict metadata; we store the id only inside the
    plugin's own data dir, 0600).
    """
    raw_id = entry.get("raw_id")
    if not raw_id:
        return False, "missing timestamps"  # no row to validate -> drop
    row = read_row(paths.db_path, str(raw_id))
    if row is None:
        return False, "row missing"
    return _validate_for_arming(row, entry, gen, paths.db_path)


def _delete_if_ready(key: str, entry: dict[str, Any], ledger: dict[str, Any],
                     settings: dict[str, Any], meta: dict[str, Any], paths: CapturedPaths,
                     enrolled: bool) -> str:
    from . import store

    raw_id = entry.get("raw_id")
    if not raw_id:
        return "refused"
    row = read_row(paths.db_path, str(raw_id))
    if row is None:
        # Someone else removed it: verify + mark deleted (external).
        store.mutate("ledger", paths.data_dir, lambda l: _mark_external_deleted(l, key))
        return "deleted"
    if not row_is_closed(row):
        return "open"  # unknown/automatic reasons and open rows stay protected
    if not enrolled:
        return "unenrolled"
    if effective_mode(settings, entry) == "off":
        return "mode_off"
    # Re-check under the store lock immediately before mutating (§3.3/§4).
    state_ok = {"ok": False}

    def check(l: dict[str, Any]) -> None:
        e = l.get(key)
        state_ok["ok"] = bool(e and e.get("state") == "armed" and e.get("override") not in {"off", "keep"})

    store.mutate("ledger", paths.data_dir, check)
    if not state_ok["ok"]:
        return "refused"
    return fenced_delete(str(raw_id), key, ledger, paths)


def _mark_external_deleted(ledger: dict[str, Any], key: str) -> dict[str, Any]:
    entry = ledger.get(key)
    if entry is not None and entry.get("state") in {"armed", "candidate"}:
        for h in store_lineage(ledger, key):
            member = ledger.get(h)
            if member is not None and member.get("state") in {"armed", "candidate"}:
                member["state"] = "deleted"
                member["deleted_how"] = "external"
    return ledger


def store_lineage(ledger: dict[str, Any], key: str) -> set:
    from . import store

    return store.lineage_hashes(ledger, key)


def fenced_delete(raw_id: str, key: str, ledger: dict[str, Any], paths: CapturedPaths) -> str:
    """The only deletion path (§3.1/§3.3). Returns 'deleted' | 'refused' | 'busy'."""
    from . import store
    from hermes_state import SessionDB

    try:
        db = SessionDB(paths.db_path)
    except Exception:
        return "refused"
    try:
        targets = db.get_session_delete_targets(raw_id)
    finally:
        try:
            db.close()
        except Exception:
            pass
    if not targets:
        return "refused"
    # Every target must be ledgered armed+closed; else refuse the WHOLE operation.
    for target in targets:
        t_entry = ledger.get(store.hash_session_id(target))
        if t_entry is None or t_entry.get("state") not in {"armed"} or t_entry.get("override") in {"off", "keep"}:
            logger.debug("ipm: delete refused (unadmitted target)")
            return "refused"
        t_row = read_row(paths.db_path, target)
        if t_row is not None and not row_is_closed(t_row):
            logger.debug("ipm: delete refused (open lineage member)")
            return "refused"
    # Release the store lock for the destructive call (its on_session_delete
    # callback takes the ledger lock); the fence re-checks inside the transaction.
    try:
        db = SessionDB(paths.db_path)
    except Exception:
        return "refused"
    try:
        deleted = db.delete_session(
            raw_id, sessions_dir=None,
            expected_delete_ids=targets,
            exclude_active_write_guards=True,
        )
    except Exception:
        try:
            db.close()
        except Exception:
            pass
        logger.debug("ipm: fenced delete raised (guard/refusal)")
        return "refused"
    finally:
        try:
            db.close()
        except Exception:
            pass
    # Verify exact-id absence before marking (§3.3).
    if not deleted or read_row(paths.db_path, raw_id) is not None:
        return "refused"
    cleanup_session_files(paths, targets)

    def mark(l: dict[str, Any]) -> dict[str, Any]:
        for t in targets:
            h = store.hash_session_id(t)
            for k in store.lineage_hashes(l, h) | {h}:
                member = l.get(k)
                if member is not None and member.get("state") in {"armed", "candidate"}:
                    member["state"] = "deleted"
                    member["deleted_at"] = time.time()
                    member["deleted_how"] = "fenced"
        return l

    store.mutate("ledger", paths.data_dir, mark)
    flush_wal(paths.db_path)
    return "deleted"


# --- files + WAL -------------------------------------------------------------

def session_filename_component(session_id: str) -> str:
    """Mirror of agent.session_persistence._safe_session_filename_component
    (pinned by contract test against core): non ``[A-Za-z0-9_-]`` -> ``_``, capped
    at 96, plus a content hash when changed."""
    import hashlib

    raw = str(session_id or "").strip()
    sanitized = re.sub(r"[^\w-]", "_", raw).strip("._")[:96] or "session"
    if raw and sanitized == raw:
        return sanitized
    return f"{sanitized}_{hashlib.sha256(raw.encode('utf-8', errors='surrogatepass')).hexdigest()[:12]}"


def cleanup_session_files(paths: CapturedPaths, ids: list[str]) -> int:
    """Remove only files whose names exactly match the Hermes writer naming for
    these ids (§3.3). Dump timestamps must match ``%Y%m%d_%H%M%S_%f`` exactly, so
    ``abc``'s pattern can never eat ``abc_extra``'s dump (contract-tested)."""
    import glob as globmod

    sessions_dir = paths.sessions_dir
    removed = 0
    for sid in ids:
        component = session_filename_component(sid)
        names = [f"{sid}.json", f"{sid}.jsonl", f"session_{sid}.json"]
        for name in names:
            p = sessions_dir / name
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        pattern = str(sessions_dir / f"request_dump_{globmod.escape(component)}_*.json")
        try:
            matches = globmod.glob(pattern)
        except OSError:
            matches = []
        prefix = f"request_dump_{component}_"
        for m in matches:
            name = Path(m).name
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            ts = name[len(prefix):-len(".json")]
            if not _ID_TS_RE.match(ts):
                continue  # another id's dump (e.g. abc_extra) — never touch it
            try:
                Path(m).unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def flush_wal(db_path: Path) -> bool:
    """RW connection, ``PRAGMA wal_checkpoint(TRUNCATE)``, check the tuple
    (§3.3). busy -> retry next sweep. Never touch ``-wal``/``-shm`` files."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        busy, _log, _checkpointed = (int(row[0]), int(row[1]), int(row[2]))
        return busy == 0
    except sqlite3.Error:
        return False


# --- finalize ----------------------------------------------------------------

NOTICE_DELETED = "🔒 Private chat removed from Hermes history."
NOTICE_QUEUED = ("🔒 Private chat queued for removal from Hermes history "
                 "(runs at your next Hermes activity or /private cleanup).")
NOTICE_KEPT = "Chat kept (private mode off or /private keep)."
NOTICE_COPIES = "Some copies remain — see /private status."


def finalize(session_id: str | None, platform: str | None, paths: CapturedPaths,
             **kwargs: Any) -> str | None:
    """on_session_finalize: mark, spawn the opportunistic sweep, render the
    notice from ACTUAL state (never hardcoded, §3.3)."""
    from . import store

    if not session_id or not store.validate_session_id(str(session_id)):
        return None
    settings = read_settings(paths)
    meta = store.read("meta", paths.data_dir)
    enrolled = cleanup_enrolled(settings, meta)
    ledger = store.read("ledger", paths.data_dir)
    entry = store.ledger_get(ledger, str(session_id))
    if entry is None:
        return None
    notice: str | None
    state = entry.get("state")
    override = entry.get("override")
    if state == "deleted":
        notice = NOTICE_DELETED if entry.get("deleted_how") == "fenced" else None
    elif state in {"armed", "candidate"} and enrolled and override not in {"off", "keep"} \
            and platform_in_cleanup_scope(str(platform or ""), settings):
        row = read_row(paths.db_path, str(session_id))
        if row is not None and row_is_closed(row):
            notice = NOTICE_QUEUED
        else:
            notice = NOTICE_QUEUED if state == "armed" else None
    else:
        notice = NOTICE_KEPT if override in {"off", "keep"} else None
    maybe_sweep(paths, trigger="finalize", force=True)
    if notice:
        return f"{notice}\n{NOTICE_COPIES}"
    return None


def cleanup_outcomes(paths: CapturedPaths) -> list[str]:
    """``hermes incognito-private-mode cleanup`` / ``/private cleanup``: run a
    sweep synchronously and print per-session outcomes (ids hashed, §3.9)."""
    from . import store

    counts = run_sweep(paths, batch_limit=MAX_BATCH)
    ledger = store.read("ledger", paths.data_dir)
    lines = [f"sweep: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v)]
    for key, entry in sorted(ledger.items()):
        if not isinstance(entry, dict):
            continue
        state = entry.get("state")
        if state == "deleted":
            lines.append(f"{key[:12]}…: deleted ({entry.get('deleted_how', 'fenced')})")
        elif state == "armed":
            lines.append(f"{key[:12]}…: queued (closed; retried on next activity)")
        elif state == "candidate":
            lines.append(f"{key[:12]}…: pending (chat not closed yet)")
    return lines


def deep_clean_report(paths: CapturedPaths) -> str:
    """``/private deep-clean`` / ``hermes incognito-private-mode deep-clean``:
    report + steps; never passes --force (§3.11)."""
    try:
        db_size = paths.db_path.stat().st_size
    except OSError:
        db_size = 0
    import shutil

    free = shutil.disk_usage(str(paths.home)).free
    lines = [
        f"state.db size: {db_size / (1024 * 1024):.1f} MB",
        f"free disk:     {free / (1024 * 1024):.1f} MB",
        "",
        "Deleted chats leave bytes in free database pages until a deep clean.",
        "Steps (run outside this app):",
        "  1. quit Desktop/CLI and run `hermes gateway stop`",
        "  2. hermes sessions optimize          # never --force",
        "  3. restart Hermes",
        "",
        "`hermes incognito-private-mode deep-clean` runs the same optimize without",
        "--force after a free-disk check and judges success from output + DB size.",
    ]
    return "\n".join(lines)
