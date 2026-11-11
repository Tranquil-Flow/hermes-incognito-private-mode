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

# Deletion-eligible close reasons. Pinned by tests/contract/test_source_pins.py
# against the citing source lines.
ALLOWED_END_REASONS = {
    "cli_close",      # hermes_cli/cli_shutdown.py:289
    "tui_close",      # tui_gateway/session_lifecycle.py:430 (explicit user close)
    "agent_close",    # run_agent.py:1025 (first reason wins)
}
# Explicit user closes that core writes but that have NO acceptance evidence in
# this build (§6 row 8 CLI /new is skipped-with-reason; /resume and /branch have
# no row at all). §6: "no default deletion on a surface whose row is not pass"
# -> these are NEVER deletion triggers (rows stay protected like open rows).
# There is deliberately no opt-in setting in this build.
EXPLICIT_UNPROVEN_END_REASONS = frozenset({
    "new_session",    # hermes_cli/cli_session_mixin.py:594 (/new)
    "resumed_other",  # hermes_cli/cli_commands_mixin.py:864 (/resume switch)
    "branched",       # hermes_cli/cli_commands_mixin.py:975 (/branch switch)
})
# Rows with these reasons on desktop stay open in Hermes and are never deleted
# automatically (tui_gateway/session_lifecycle.py:60).
AUTOMATIC_END_REASONS = {"ws_orphan_reap", "ws_disconnect", "idle_timeout", "lru_evict", "tui_shutdown"}

MAX_BATCH = 20
SWEEP_MIN_INTERVAL = 10 * 60  # rate-limited llm_request trigger (§3.3)
START_SWEEP_MIN_INTERVAL = 30  # on_session_start trigger
MAX_ROW_READS = 500  # per-sweep bound on eligibility row reads
LINEAGE_READ_CAP = 200  # per-notice bound on lineage row reads
_ID_TS_RE = re.compile(r"^\d{8}_\d{6}_\d{6}$")


# --- read-only row helpers ---------------------------------------------------

def _ro_connect(db_path: Path) -> sqlite3.Connection:
    # as_uri() percent-encodes '#', '?' and '%' — a raw f"file:{path}" URI
    # truncates at the first '#' and can silently query the wrong file.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# Row-read outcomes: 'present' | 'absent' | 'error'. ABSENCE AND ERROR ARE NOT
# THE SAME THING (audit fix): an unreadable DB must never be treated as a
# deleted row, or entries get falsely tombstoned while their data survives.
def store_validate_id(session_id: str) -> bool:
    from .store import validate_session_id
    try:
        return bool(validate_session_id(session_id))
    except Exception:
        return False


def _db_file_ok(db_path: Path) -> bool:
    try:
        return Path(db_path).is_file()
    except (OSError, TypeError, ValueError):
        return False


def read_row_checked(db_path: Path, session_id: str) -> tuple[str, dict[str, Any] | None]:
    """ONLY a successful exact-id query on an existing, readable DB proves
    absence. A missing/unreadable DB, a missing table, or an id that fails the
    exact-id pattern proves nothing -> 'error' (callers then change nothing).
    Read-only URI: a read never creates the DB."""
    if not store_validate_id(session_id) or not _db_file_ok(db_path):
        return "error", None
    try:
        conn = _ro_connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, started_at, ended_at, end_reason, source, parent_session_id "
                "FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return "error", None
    return ("present", dict(row)) if row else ("absent", None)


def read_row(db_path: Path, session_id: str) -> dict[str, Any] | None:
    state, row = read_row_checked(db_path, session_id)
    return row if state == "present" else None


def min_message_ts_checked(db_path: Path, session_id: str) -> tuple[str, float | None]:
    """('ok', earliest message ts or None) | ('error', None). An unreadable DB
    must never read as 'no older messages' (that would arm the chat)."""
    if not store_validate_id(session_id) or not _db_file_ok(db_path):
        return "error", None
    try:
        conn = _ro_connect(db_path)
        try:
            row = conn.execute(
                "SELECT MIN(timestamp) AS t FROM messages WHERE session_id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return "error", None
    if row is None or row["t"] is None:
        return "ok", None
    return "ok", float(row["t"])


def min_message_ts(db_path: Path, session_id: str) -> float | None:
    _state, ts = min_message_ts_checked(db_path, session_id)
    return ts


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
    """on_session_start: create a candidate when cleanup could ever apply (§3.3).

    A chat born while the effective mode is off is EXCLUDED forever (audit fix):
    the consent text promises "chats created while off survive", and a later
    mode flip must never retro-arm them. A mode change also closes the current
    arming generation and opens a new one, so generation matching blocks arming
    for anything straddling the flip.
    """
    from . import store

    if not session_id or not store.validate_session_id(str(session_id)):
        return
    settings = read_settings(paths)
    meta = store.read("meta", paths.data_dir)
    _maybe_generation_on_mode_change(settings, meta, paths)
    meta = store.read("meta", paths.data_dir)
    gen = store.current_generation(meta)
    generation = int(gen["n"]) if gen else None
    enrolled = cleanup_enrolled(settings, meta)
    platform = str(platform or "")
    mode_now = effective_mode(settings, None)

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        key = store.hash_session_id(str(session_id))
        existing = ledger.get(key)
        if existing is not None:
            return ledger
        if not enrolled or not platform_in_cleanup_scope(platform, settings) \
                or settings.get("keep_history") or generation is None \
                or mode_now != "private":
            if mode_now != "private":
                reason = "mode off at creation (survives forever)"
            elif not enrolled:
                reason = "cleanup not enrolled"
            elif not platform_in_cleanup_scope(platform, settings):
                reason = "platform out of cleanup scope"
            else:
                reason = "keep_history=true at creation"
            ledger[key] = {
                "state": "excluded",
                "excluded_reason": reason,
                "platform": platform,
                "first_seen": time.time(),
            }
            return ledger
        ledger[key] = _entry(ledger, str(session_id), platform, generation, None, time.time())
        return ledger

    store.mutate("ledger", paths.data_dir, fn)
    maybe_sweep(paths, trigger="session_start")


def _gen_mode(gen: dict[str, Any]) -> str:
    """Mode recorded on a generation; pre-field generations infer it from armed."""
    mode = gen.get("mode")
    if mode in {"private", "off"}:
        return str(mode)
    return "private" if gen.get("armed") else "off"


def _maybe_generation_on_mode_change(settings: dict[str, Any], meta: dict[str, Any],
                                     paths: CapturedPaths) -> None:
    """When the effective mode differs from the last generation's mode, close
    that generation and open a new one (armed iff mode == private). Cheap
    check on a lock-free meta read; the append itself is a locked RMW, so this
    must NEVER be called from inside another store.mutate callback (a second
    flock on the store lock in the same process self-deadlocks)."""
    from . import store

    try:
        gen = store.current_generation(meta)
        if gen is None:
            return
        gen_mode = _gen_mode(gen)
        mode_now = effective_mode(settings, None)
        if gen_mode == mode_now:
            return

        def apply(m: dict[str, Any]) -> dict[str, Any]:
            latest = store.current_generation(m)
            if latest is not None and _gen_mode(latest) == mode_now:
                return m  # another process recorded the same flip first
            m2 = store.new_generation(m, armed=(mode_now == "private"),
                                      reason=f"mode -> {mode_now}", now=time.time())
            new_gen = store.current_generation(m2)
            if isinstance(new_gen, dict):
                new_gen["mode"] = mode_now
            return m2

        store.mutate("meta", paths.data_dir, apply)
    except Exception:
        logger.debug("ipm: mode-change generation skipped")


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
    """post_compaction: link the continuation to its predecessor (§3.1/§3.3).

    Audit fixes: (1) an OLD id we never ledgered is unknown provenance — the
    continuation is tombstoned excluded instead of silently joining the current
    generation; (2) keep/off exclusions INHERIT down the lineage, so a kept
    predecessor can never have a deletable continuation."""
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
            # Unknown provenance: protect the continuation permanently.
            entry = ledger.get(new_hash)
            if entry is None or entry.get("state") in {"candidate", "armed"}:
                ledger[new_hash] = {
                    "state": "excluded",
                    "excluded_reason": "predecessor provenance unknown (pre-consent chat)",
                    "parent_hash": old_hash,
                    "first_seen": time.time(),
                }
            return ledger
        entry = ledger.get(new_hash)
        protected = old.get("state") == "excluded" or old.get("override") in {"keep", "off"}
        if entry is None:
            entry = dict(old)
            entry.update({"parent_hash": old_hash, "created_ts": time.time(),
                          "raw_id": str(session_id), "children": [], "attempts": 0})
            if entry["state"] != "excluded":
                entry["state"] = "candidate"
            ledger[new_hash] = entry
        else:
            entry["parent_hash"] = old_hash
        if protected and entry.get("state") != "deleted":
            entry["state"] = "excluded"
            entry["excluded_reason"] = (
                "lineage /private keep" if old.get("override") == "keep"
                else "lineage /private off" if old.get("override") == "off"
                else "predecessor excluded")
            if old.get("override") in {"keep", "off"}:
                entry["override"] = old["override"]
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
    """on_session_delete: invalidation hint ONLY (§4) — fires for archive too,
    and ALSO for our own fenced delete (core fires the hook inside
    delete_session). Verify row state before marking (exact-id absence =
    externally removed). Marks ONLY the single key — never a lineage (audit
    fix: core orphans compression parents instead of deleting them, so a
    surviving parent must never be recorded as deleted)."""
    from . import store

    if not session_id or not store.validate_session_id(str(session_id)):
        return
    key = store.hash_session_id(str(session_id))
    state, _row = read_row_checked(paths.db_path, str(session_id))

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        entry = ledger.get(key)
        if entry is None:
            return ledger
        if entry.get("deleting"):
            return ledger  # our own fenced delete is in flight — it marks 'fenced'
        if state == "absent":
            if entry.get("state") in {"armed", "candidate"}:
                entry["state"] = "deleted"
                entry["deleted_how"] = "external"
                entry["deleted_at"] = time.time()
                entry.pop("raw_id", None)  # deleted tombstones keep no raw ids
        elif state == "error":
            pass  # unreadable DB proves nothing — leave the ledger unchanged
        elif reason == "archive" and entry.get("state") in {"armed", "candidate"}:
            entry["state"] = "excluded"
            entry["excluded_reason"] = "archived by user"
        return ledger

    store.mutate("ledger", paths.data_dir, fn)


# --- admission policy ----------------------------------------------------------

# Refusal / exclusion reasons that truthfully render NOTICE_KEPT.
_KEPT_REASONS = frozenset({
    "private mode off", "/private off", "/private keep", "observed with private mode off",
    "lineage member kept or observed off", "mode off at creation (survives forever)",
    "lineage /private keep", "lineage /private off", "user kept this chat",
})
# Row-level arming refusals that can never resolve later -> permanent tombstone.
_PERMANENT_ARMING_REASONS = frozenset({
    "row predates the arming generation", "messages predate the arming generation",
    "parent provenance unadmitted", "parent from another generation",
})
# Policy refusals that prove nothing about the chat (unreadable/never-set-up
# meta, unreadable lineage): refuse now, never tombstone.
_TRANSIENT_POLICY_REASONS = frozenset({"no arming generation", "lineage unreadable"})


def _policy_refusal(key: str, entry: dict[str, Any] | None, settings: dict[str, Any],
                    meta: dict[str, Any], ledger: dict[str, Any]) -> str | None:
    """Ledger/settings-only admission for deleting (or arming) one entry — no
    DB access. None = admitted. Every reason outside _TRANSIENT_POLICY_REASONS
    is PERMANENT: callers tombstone the entry excluded, so a later settings flip
    back can never widen deletion to a chat that lived through it (round-3 HIGH:
    gen1 armed -> mode off -> close -> mode on was deleted because only
    candidates were generation-checked).

    Requires: an arming generation on record, consent + remove_history_on_close,
    global mode private, chat effective mode private, keep_history false, no
    keep/off override, never observed off, platform in cleanup scope, the
    CURRENT generation armed in private mode and equal to the entry's
    generation, and no lineage member observed off or kept."""
    from . import store

    if not isinstance(entry, dict):
        return "not ledgered"
    gen = store.current_generation(meta)
    if gen is None:
        return "no arming generation"
    if not cleanup_enrolled(settings, meta):
        return "cleanup not enrolled"
    if settings.get("mode") != "private":
        return "private mode off"
    if settings.get("keep_history"):
        return "keep_history=true"
    override = entry.get("override")
    if override in {"off", "keep"}:
        return f"/private {override}"
    if effective_mode(settings, entry) != "private":
        return "private mode off"
    if entry.get("observed_off"):
        return "observed with private mode off"
    if not platform_in_cleanup_scope(str(entry.get("platform") or ""), settings):
        return "platform out of cleanup scope"
    if not gen.get("armed") or _gen_mode(gen) != "private":
        return "arming generation disarmed"
    if entry.get("generation") != gen.get("n"):
        return "arming generation changed"
    try:
        lineage = store.lineage_hashes(ledger, key)
    except Exception:
        return "lineage unreadable"
    for h in lineage:
        member = ledger.get(h)
        if isinstance(member, dict) and (member.get("observed_off")
                                         or member.get("override") in {"off", "keep"}):
            return "lineage member kept or observed off"
    return None


def _exact_raw_id(key: str, entry: dict[str, Any]) -> str | None:
    """The entry's raw id only when it passes exact-id validation AND hashes to
    its own ledger key (never act on a mismatched id)."""
    from . import store

    raw_id = entry.get("raw_id") if isinstance(entry, dict) else None
    if not store_validate_id(raw_id) or store.hash_session_id(str(raw_id)) != key:
        return None
    return str(raw_id)


def _validate_row_for_arming(row: dict[str, Any], entry: dict[str, Any], gen: dict[str, Any] | None,
                             db_path: Path, ledger: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Row-level candidate -> armed checks (§3.3): whole life inside the arming
    generation, no message older than it, and EVERY lineage parent an admitted
    ledger member of the same generation."""
    from .store import hash_session_id

    if gen is None:
        return False, "no arming generation"
    started = row.get("started_at")
    gen_start = gen.get("started_at")
    if not isinstance(started, (int, float)) or not isinstance(gen_start, (int, float)):
        return False, "missing timestamps"
    if float(started) < float(gen_start):
        return False, "row predates the arming generation"
    m_state, mts = min_message_ts_checked(db_path, str(row["id"]))
    if m_state != "ok":
        return False, "db unreadable"  # transient; never read as 'no older messages'
    if mts is not None and mts < float(gen_start):
        return False, "messages predate the arming generation"
    parent_id = row.get("parent_session_id")
    if parent_id and ledger is not None:
        parent = ledger.get(hash_session_id(str(parent_id)))
        if parent is None or parent.get("state") == "excluded":
            return False, "parent provenance unadmitted"
        pgen = parent.get("generation")
        if pgen is not None and pgen != entry.get("generation"):
            return False, "parent from another generation"
    return True, ""


def _validate_for_arming_from_ledger(key: str, entry: dict[str, Any], settings: dict[str, Any],
                                     meta: dict[str, Any], ledger: dict[str, Any],
                                     paths: CapturedPaths) -> tuple[bool, str, bool]:
    """Candidate validation -> (ok, why, permanent). Policy first (no DB), then
    the exact-id row. The raw id lives only in the plugin's own 0600 data dir and
    is dropped once the entry is deleted."""
    from . import store

    why = _policy_refusal(key, entry, settings, meta, ledger)
    if why:
        return False, why, why not in _TRANSIENT_POLICY_REASONS
    raw_id = _exact_raw_id(key, entry)
    if raw_id is None:
        return False, "no exact session id", True
    state, row = read_row_checked(paths.db_path, raw_id)
    if state == "error":
        return False, "db unreadable", False  # proves nothing; retry later
    if state == "absent" or row is None:
        return False, "row missing", False  # may not be flushed yet
    ok, why = _validate_row_for_arming(row, entry, store.current_generation(meta), paths.db_path, ledger)
    return ok, why, (not ok and why in _PERMANENT_ARMING_REASONS)


def _exclude_members(ledger: dict[str, Any], hashes: set, why: str) -> None:
    for h in hashes:
        member = ledger.get(h)
        if isinstance(member, dict) and member.get("state") in {"candidate", "armed"} \
                and not member.get("deleting"):
            member["state"] = "excluded"
            member["excluded_reason"] = why


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
    """One opportunistic sweep. Non-blocking flock; bounded batch; content-free logs.

    1. Record a global mode change as a generation at sweep START (standalone
       meta RMW — never nested in another mutate).
    2. Residue recovery for interrupted fenced deletes.
    3. Candidates -> armed / permanently excluded; armed entries whose policy
       no longer admits them -> permanently excluded. Merged per FIELD under the
       lock with a fresh policy re-check (a concurrent observed_off/override is
       never overwritten by this sweep's snapshot).
    4. Only armed rows that are closed by an admitted path (or verifiably
       absent) take batch slots, so open or unadmitted-close rows cannot starve
       deletable ones. Each goes through fenced_delete's own fresh admission.
    """
    from . import store

    counts: dict[str, int] = {"attempted": 0, "deleted": 0, "refused": 0, "busy": 0, "armed": 0,
                              "dropped": 0}
    with store.sweep_lock(paths.data_dir) as acquired:
        if not acquired:
            return {"skipped_locked": 1}
        settings = read_settings(paths)
        _maybe_generation_on_mode_change(settings, store.read("meta", paths.data_dir), paths)
        meta = store.read("meta", paths.data_dir)
        ledger = store.read("ledger", paths.data_dir)
        # key -> (kind, fields to set, fields to pop)
        updates: dict[str, tuple[str, dict[str, Any], tuple[str, ...]]] = {}

        # Residue recovery: a previous sweep may have died between the fenced
        # delete and its marking. 'deleting' entries whose row is now verifiably
        # absent get their session FILES removed, then tombstoned.
        for key, entry in ledger.items():
            if not isinstance(entry, dict) or not entry.get("deleting"):
                continue
            raw_id = _exact_raw_id(key, entry)
            if raw_id is None:
                continue  # cannot verify anything for this entry: change nothing
            state, _row = read_row_checked(paths.db_path, raw_id)
            if state == "absent":
                cleanup_session_files(paths, [raw_id])
                updates[key] = ("residue", {"state": "deleted", "deleted_how": "fenced",
                                            "deleted_at": time.time()}, ("deleting", "raw_id"))
            elif state == "present":
                updates[key] = ("residue", {}, ("deleting",))  # row is back: keep data, drop intent
            # 'error': unreadable DB proves nothing — retry on a later sweep

        armed_keys: list[str] = []
        for key, entry in ledger.items():
            if not isinstance(entry, dict) or entry.get("deleting") or key in updates:
                continue
            state = entry.get("state")
            if state == "candidate":
                ok, why, permanent = _validate_for_arming_from_ledger(
                    key, entry, settings, meta, ledger, paths)
                if ok:
                    updates[key] = ("arm", {"state": "armed"}, ())
                elif permanent:
                    updates[key] = ("exclude", {"state": "excluded", "excluded_reason": why}, ())
            elif state == "armed":
                why = _policy_refusal(key, entry, settings, meta, ledger)
                if why and why not in _TRANSIENT_POLICY_REASONS:
                    updates[key] = ("exclude", {"state": "excluded", "excluded_reason": why}, ())
                elif not why:
                    armed_keys.append(key)

        applied: dict[str, str] = {}
        if updates:
            def apply(l: dict[str, Any]) -> dict[str, Any]:
                for k, (kind, set_fields, pops) in updates.items():
                    cur = l.get(k)
                    if not isinstance(cur, dict):
                        continue
                    if kind == "residue":
                        if not cur.get("deleting"):
                            continue
                    elif cur.get("deleting") or cur.get("state") != (ledger.get(k) or {}).get("state"):
                        continue  # changed since the snapshot: leave it to the next sweep
                    elif kind == "arm":
                        why_now = _policy_refusal(k, cur, settings, meta, l)
                        if why_now in _TRANSIENT_POLICY_REASONS:
                            continue  # proves nothing: stay a candidate
                        if why_now:  # e.g. observed_off landed after validation
                            cur["state"] = "excluded"
                            cur["excluded_reason"] = why_now
                            applied[k] = "exclude"
                            continue
                    cur.update(set_fields)
                    for field in pops:
                        cur.pop(field, None)
                    applied[k] = kind
                return l  # mutate persists ONLY the returned dict

            store.mutate("ledger", paths.data_dir, apply)
        counts["armed"] = sum(1 for v in applied.values() if v == "arm")
        counts["dropped"] = sum(1 for v in applied.values() if v == "exclude")
        armed_keys.extend(k for k, v in applied.items() if v == "arm")

        eligible: list[str] = []
        reads = 0
        for key in armed_keys:
            if len(eligible) >= batch_limit or reads >= MAX_ROW_READS:
                break
            raw_id = _exact_raw_id(key, ledger.get(key) or {})
            if raw_id is None:
                continue
            reads += 1
            state, row = read_row_checked(paths.db_path, raw_id)
            if state == "absent" or (state == "present" and row_is_closed(row)):
                eligible.append(key)
            elif state == "present":
                counts["open"] = counts.get("open", 0) + 1  # open or unadmitted close: protected
            else:
                counts["refused"] += 1  # unreadable DB: nothing proven, retry later
        counts["attempted"] = len(eligible)
        for key in eligible:
            outcome = _delete_if_ready(key, paths)
            counts[outcome] = counts.get(outcome, 0) + 1

        store.mutate("meta", paths.data_dir, lambda m: {**m, "last_sweep": {"at": time.time(),
                     **{k: v for k, v in counts.items()}}})
    return counts


def _delete_if_ready(key: str, paths: CapturedPaths) -> str:
    from . import store

    entry = store.read("ledger", paths.data_dir).get(key)
    if not isinstance(entry, dict) or entry.get("state") != "armed" or entry.get("deleting"):
        return "refused"
    raw_id = _exact_raw_id(key, entry)
    if raw_id is None:
        return "refused"
    state, row = read_row_checked(paths.db_path, raw_id)
    if state == "error":
        # Unreadable DB proves nothing: refuse and leave the ledger unchanged.
        return "refused"
    if state == "absent" or row is None:
        # Someone else removed it (verified on a readable DB): this key only.
        store.mutate("ledger", paths.data_dir, lambda l: _mark_external_deleted(l, key))
        return "external"
    if not row_is_closed(row):
        return "open"  # open rows, unknown/automatic/unproven reasons stay protected
    return fenced_delete(raw_id, key, None, paths)


def _mark_external_deleted(ledger: dict[str, Any], key: str) -> dict[str, Any]:
    entry = ledger.get(key)
    if entry is not None and entry.get("state") in {"armed", "candidate"} \
            and not entry.get("deleting"):
        entry["state"] = "deleted"
        entry["deleted_how"] = "external"
        entry["deleted_at"] = time.time()
        entry.pop("raw_id", None)
    return ledger


def store_lineage(ledger: dict[str, Any], key: str) -> set:
    from . import store

    return store.lineage_hashes(ledger, key)


def _fence_admission(ledger: dict[str, Any], key: str, targets: list[str], settings: dict[str, Any],
                     meta: dict[str, Any], paths: CapturedPaths) -> tuple[str | None, bool]:
    """Fresh admission of the WHOLE fence target set -> (refusal | None, permanent).
    Runs inside the ledger RMW: read-only on settings/meta/DB, never mutates meta."""
    from . import store

    root = ledger.get(key)
    if not isinstance(root, dict) or root.get("state") != "armed":
        return "chat no longer armed", False
    for target in targets:
        th = store.hash_session_id(target)
        te = ledger.get(th)
        if not isinstance(te, dict):
            return "unledgered lineage member", True
        if te.get("deleting"):
            return "member delete already in flight", False
        if te.get("state") == "candidate":
            return "lineage member not armed yet", False
        if te.get("state") != "armed":
            return "lineage member protected", True
        why = _policy_refusal(th, te, settings, meta, ledger)
        if why:
            return why, why not in _TRANSIENT_POLICY_REASONS
        t_state, t_row = read_row_checked(paths.db_path, target)
        if t_state != "present" or t_row is None or not row_is_closed(t_row):
            return "lineage member not closed by an admitted path", False
    return None, False


def fenced_delete(raw_id: str, key: str, ledger: dict[str, Any] | None, paths: CapturedPaths) -> str:
    """The only deletion path (§3.1/§3.3). Returns 'deleted' | 'refused'.

    *ledger* is accepted for call compatibility and IGNORED: admission is
    decided only from FRESH state.

    0. Targets + DB handle are prepared OUTSIDE the plugin's store lock.
    1. Under the ledger lock: re-read settings, consent and the generation
       (lock-free reads; nothing here mutates meta — nested flocks deadlock)
       and admit EVERY target against the fresh ledger (_fence_admission). A
       permanent refusal tombstones the lineage excluded (never retried, never
       widened later). Admission sets a 'deleting' intent flag on every target
       (our own on_session_delete hook, fired inside delete_session, skips them).
    2. Lock released: the fenced destructive call (expected-ids fence).
    3. Verify exact-id absence per target; mark ONLY verified-absent targets.
    """
    from . import store
    from hermes_state import SessionDB

    if not store_validate_id(raw_id) or store.hash_session_id(str(raw_id)) != key:
        return "refused"
    try:
        db = SessionDB(paths.db_path)
    except Exception:
        return "refused"
    decision: dict[str, Any] = {"ok": False, "why": ""}
    try:
        try:
            targets = [str(t) for t in (db.get_session_delete_targets(raw_id) or [])]
        except Exception:
            return "refused"
        if not targets or targets[0] != raw_id or len(set(targets)) != len(targets) \
                or not all(store_validate_id(t) for t in targets):
            return "refused"

        def check(l: dict[str, Any]) -> dict[str, Any]:
            try:
                fresh_settings = read_settings(paths)
                fresh_meta = store.read("meta", paths.data_dir)
                why, permanent = _fence_admission(l, key, targets, fresh_settings, fresh_meta, paths)
            except Exception:
                why, permanent = "admission check failed", False
            if why:
                decision["why"] = why
                if permanent:
                    members = set(store.lineage_hashes(l, key))
                    members |= {store.hash_session_id(t) for t in targets}
                    _exclude_members(l, members, why)
                return l
            decision["ok"] = True
            for target in targets:
                l[store.hash_session_id(target)]["deleting"] = True
            return l  # mutate persists the dict it returns — flags must survive

        store.mutate("ledger", paths.data_dir, check)
        if not decision["ok"]:
            logger.debug("ipm: delete refused (%s)", decision["why"])  # fixed vocabulary, no ids
            return "refused"
        try:
            deleted = db.delete_session(
                raw_id, sessions_dir=None,
                expected_delete_ids=targets,
                exclude_active_write_guards=True,
            )
        except Exception:
            self_clear(paths, [store.hash_session_id(t) for t in targets])
            logger.debug("ipm: fenced delete raised (guard/refusal)")
            return "refused"
    finally:
        try:
            db.close()
        except Exception:
            pass
    if not deleted:
        self_clear(paths, [store.hash_session_id(t) for t in targets])
        return "refused"
    # 3. Verify exact-id absence before marking (§3.3).
    states = {t: read_row_checked(paths.db_path, t)[0] for t in targets}
    gone = [t for t in targets if states[t] == "absent"]
    still = [t for t in targets if states[t] == "present"]
    if still:
        self_clear(paths, [store.hash_session_id(t) for t in still])
    # 'error' targets keep their intent flag: residue recovery verifies them later.
    if gone:
        cleanup_session_files(paths, gone)
        gone_hashes = {store.hash_session_id(t) for t in gone}

        def mark(l: dict[str, Any]) -> dict[str, Any]:
            for h in gone_hashes:
                member = l.get(h)
                if isinstance(member, dict) and member.get("deleting"):
                    member["state"] = "deleted"
                    member["deleted_at"] = time.time()
                    member["deleted_how"] = "fenced"
                    member.pop("deleting", None)
                    member.pop("raw_id", None)  # deleted tombstones keep no raw ids
            return l

        store.mutate("ledger", paths.data_dir, mark)
        flush_wal(paths.db_path)
    return "deleted" if raw_id in gone else "refused"


def self_clear(paths: CapturedPaths, hashes: list[str]) -> None:
    """Drop the 'deleting' intent flags (validation or delete failed; the data
    stays, so the intent must not linger)."""
    from . import store

    def clear(l: dict[str, Any]) -> dict[str, Any]:
        for h in hashes:
            member = l.get(h)
            if isinstance(member, dict):
                member.pop("deleting", None)
        return l

    try:
        store.mutate("ledger", paths.data_dir, clear)
    except Exception:
        logger.debug("ipm: deleting-flag clear failed")


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
            if not p.exists():
                continue  # count only real removals, never attempts
            try:
                p.unlink()
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
NOTICE_NOT_ENABLED = ("Chat kept in Hermes history (removal is not enabled for this way "
                      "of closing a chat).")
NOTICE_SETUP = ("Chat kept in Hermes history — run `hermes incognito-private-mode setup` "
                "to enable history removal.")
NOTICE_COPIES = "Some copies remain — see /private status."


def finalize(session_id: str | None, platform: str | None, paths: CapturedPaths,
             **kwargs: Any) -> str | None:
    """on_session_finalize: record a mode flip (cheap meta RMW, only when it
    changed), spawn the opportunistic sweep in the background, and render the
    notice from FRESH state (never hardcoded, never from a stale snapshot, §3.3).
    No sweep or delete ever runs inline here."""
    from . import store

    if not session_id or not store_validate_id(str(session_id)):
        return None
    sid = str(session_id)
    notice: str | None = None
    try:
        settings = read_settings(paths)
        _maybe_generation_on_mode_change(settings, store.read("meta", paths.data_dir), paths)
        meta = store.read("meta", paths.data_dir)
        ledger = store.read("ledger", paths.data_dir)
        notice = _render_finalize_notice(sid, str(platform or ""), settings, meta, ledger, paths,
                                         kwargs.get("reason"))
    except Exception:
        logger.debug("ipm: finalize notice skipped")
        notice = None
    maybe_sweep(paths, trigger="finalize", force=True)
    return notice


def _kept_notice(entry: dict[str, Any], settings: dict[str, Any], why: str | None) -> str | None:
    """NOTICE_KEPT only when its stated reason (mode off / keep) is the real one."""
    reasons = {why, entry.get("excluded_reason")}
    if entry.get("override") in {"off", "keep"} or entry.get("observed_off") \
            or effective_mode(settings, entry) == "off" or reasons & _KEPT_REASONS:
        return f"{NOTICE_KEPT}\n{NOTICE_COPIES}"
    return None


def _render_finalize_notice(sid: str, platform: str, settings: dict[str, Any], meta: dict[str, Any],
                            ledger: dict[str, Any], paths: CapturedPaths, reason: Any) -> str | None:
    from . import store

    key = store.hash_session_id(sid)
    entry = ledger.get(key)
    consent = meta.get("consent") if isinstance(meta, dict) else None
    if not (isinstance(consent, dict) and consent.get("recorded") is True):
        # First run without setup (§3.9): nothing is ever removed — say how to enable it.
        if settings.get("remove_history_on_close") and platform_in_cleanup_scope(platform, settings) \
                and effective_mode(settings, entry if isinstance(entry, dict) else None) == "private":
            return NOTICE_SETUP
        return None
    if not isinstance(entry, dict):
        return None
    state = entry.get("state")
    if state == "deleted":
        if entry.get("deleted_how") == "fenced":
            return _with_lineage(NOTICE_DELETED, ledger, sid, paths)
        return None
    if state == "excluded":
        return _kept_notice(entry, settings, None)
    if state not in {"armed", "candidate"} or entry.get("deleting"):
        return None  # in flight / unknown: claim nothing
    if state == "candidate":
        ok, why, _permanent = _validate_for_arming_from_ledger(key, entry, settings, meta, ledger, paths)
        if not ok:
            return _kept_notice(entry, settings, why)
    else:
        why = _policy_refusal(key, entry, settings, meta, ledger)
        if why:
            return _kept_notice(entry, settings, why)
    # Admitted under fresh state: the close path decides what we may claim.
    row_state, row = read_row_checked(paths.db_path, sid)
    if row_state != "present" or row is None:
        return None
    if row.get("ended_at") is not None:
        if not row_is_closed(row):
            return f"{NOTICE_NOT_ENABLED}\n{NOTICE_COPIES}"
    elif reason == "session_boundary":
        # CLI /new fires finalize BEFORE ending the row 'new_session', an
        # unproven close path that is never a deletion trigger.
        return f"{NOTICE_NOT_ENABLED}\n{NOTICE_COPIES}"
    return _with_lineage(NOTICE_QUEUED, ledger, sid, paths)


def _with_lineage(head: str, ledger: dict[str, Any], sid: str, paths: CapturedPaths) -> str:
    """Append a truthful, count-based disclosure of tracked relatives that are
    not confirmed removed. No explanation of WHY is claimed."""
    parts = _remaining_lineage_parts(ledger, sid, paths)
    lines = [head]
    for label, present_n, unchecked_n in (
            ("earlier", parts["earlier"], parts["earlier_unchecked"]),
            ("other tracked", parts["other"], parts["other_unchecked"])):
        total = present_n + unchecked_n
        if not total:
            continue
        if unchecked_n:
            lines.append(f"⚠ {total} {label} part(s) of this chat remain or could not be "
                         "checked (not confirmed removed).")
        else:
            lines.append(f"⚠ {total} {label} part(s) of this chat remain "
                         "(not yet removed from Hermes history).")
    lines.append(NOTICE_COPIES)
    return "\n".join(lines)


def _remaining_lineage_parts(ledger: dict[str, Any], session_id: str,
                             paths: CapturedPaths) -> dict[str, int]:
    """Count the ENTIRE ledger lineage of this chat (fresh ledger passed in),
    excluding the chat itself and members tombstoned 'deleted' (verified absent
    when marked). 'earlier' = ancestors via parent links; 'other' = everyone
    else (delegate children, siblings). A row verifiably ABSENT on a readable DB
    is not counted; a present row counts; an unreadable/missing DB or a member
    without a checkable exact id is 'unchecked' (never treated as absent)."""
    from . import store

    parts = {"earlier": 0, "earlier_unchecked": 0, "other": 0, "other_unchecked": 0}
    key = store.hash_session_id(session_id)
    entry = ledger.get(key)
    if not isinstance(entry, dict):
        return parts
    lineage = set(store.lineage_hashes(ledger, key))
    lineage.discard(key)
    ancestors: set = set()
    cursor: dict[str, Any] = entry
    while len(ancestors) <= len(ledger):
        parent = cursor.get("parent_hash")
        if not parent or parent in ancestors or parent == key or parent not in ledger:
            break
        ancestors.add(parent)
        cursor = ledger.get(parent) if isinstance(ledger.get(parent), dict) else {}
    for i, h in enumerate(sorted(lineage)):
        rel = ledger.get(h)
        if not isinstance(rel, dict) or rel.get("state") == "deleted":
            continue
        group = "earlier" if h in ancestors else "other"
        raw_id = _exact_raw_id(h, rel)
        if raw_id is None or i >= LINEAGE_READ_CAP:
            parts[f"{group}_unchecked"] += 1
            continue
        row_state, _row = read_row_checked(paths.db_path, raw_id)
        if row_state == "absent":
            continue
        parts[group if row_state == "present" else f"{group}_unchecked"] += 1
    return parts


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
            raw_id = _exact_raw_id(key, entry)
            row_state, row = read_row_checked(paths.db_path, raw_id) if raw_id else ("error", None)
            if row_state == "present" and row is not None and row.get("ended_at") is not None \
                    and not row_is_closed(row):
                lines.append(f"{key[:12]}…: kept (closed in a way not enabled for removal)")
            elif row_state == "present" and row is not None and row_is_closed(row):
                lines.append(f"{key[:12]}…: queued (closed; retried on next activity)")
            elif row_state == "error":
                lines.append(f"{key[:12]}…: queued (history database not readable; retried later)")
            else:
                lines.append(f"{key[:12]}…: queued (waiting to close)")
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
        "`hermes incognito-private-mode deep-clean` PRINTS this report only — it",
        "does not run the optimize itself (that needs Hermes stopped, see above).",
    ]
    return "\n".join(lines)
