"""Round-3 lifecycle regressions (fresh-eyes review, 2026-11-11).

1. HIGH  armed chat (gen1) -> global mode off (gen2) -> close -> private (gen3)
         was deleted: the ARMED path never re-checked the generation, the
         observed-off marker, scope, keep_history or consent. The fence now
         re-reads settings + consent + generation under the store lock.
2.       finalize rendered 'queued' for chats the sweep then excluded, and gave
         no setup hint on a first run without enrollment.
3.       lineage disclosure: whole fresh lineage, known-absent rows not counted,
         no hardcoded 'compressed sections' explanation; broken legacy helper
         removed.
4.       read_row_checked: missing/unreadable DB and invalid ids are ERRORS,
         never absence (absence tombstones entries as externally deleted).
5.       /new (new_session), resumed_other, branched have no acceptance
         evidence -> never deletion triggers; automatic reasons stay protected.

Every deletion test drives the REAL SessionDB schema and the real fenced
delete; a spy records every destructive call so 'survives' is never vacuous
(each family also carries a positive control that IS deleted).
"""

from __future__ import annotations

import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from conftest import FakePaths
from ipm import lifecycle, store
from ipm.settings import DEFAULTS
from ipm.store import hash_session_id as H

DELEGATE = '{{"_delegate_from": "{}"}}'


# --- helpers ------------------------------------------------------------------

def make_db(paths, rows):
    """REAL SessionDB schema, then rows (id, source, parent, started, ended,
    reason[, model_config])."""
    from hermes_state import SessionDB

    db = SessionDB(paths.db_path)
    db.close()
    conn = sqlite3.connect(str(paths.db_path))
    for row in rows:
        model_config = row[6] if len(row) > 6 else None
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, parent_session_id, started_at, "
            "ended_at, end_reason, model_config) VALUES (?,?,?,?,?,?,?)",
            (*row[:6], model_config))
    conn.commit()
    conn.close()


def add_message(paths, sid, ts, text="m"):
    conn = sqlite3.connect(str(paths.db_path))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                 (sid, "user", text, ts))
    conn.commit()
    conn.close()


def close_row(paths, sid, reason, at=None):
    conn = sqlite3.connect(str(paths.db_path))
    conn.execute("UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                 (at or time.time() + 50, reason, sid))
    conn.commit()
    conn.close()


def entry(paths, sid):
    return store.read("ledger", paths.data_dir).get(H(sid)) or {}


def gen(paths):
    return store.current_generation(store.read("meta", paths.data_dir)) or {}


def present(paths, sid):
    state, _row = lifecycle.read_row_checked(paths.db_path, sid)
    assert state != "error", "test DB must be readable"
    return state == "present"


def mark_observed_off(paths, sid):
    """Exactly what enforce.on_llm_request persists on an OFF observation."""
    def fn(l):
        e = l.get(H(sid))
        if isinstance(e, dict) and e.get("state") in {"armed", "candidate"}:
            e["observed_off"] = True
        return l
    store.mutate("ledger", paths.data_dir, fn)


def _install(monkeypatch, paths, holder):
    monkeypatch.setattr(lifecycle, "read_settings", lambda p=None: dict(holder))
    sweeps: list[str] = []

    def fake_maybe_sweep(p, trigger="session_start", force=False):
        sweeps.append(trigger)  # hooks never race a daemon sweep in tests
        return {"recorded": 1}

    monkeypatch.setattr(lifecycle, "maybe_sweep", fake_maybe_sweep)
    from hermes_state import SessionDB

    deletes: list[str] = []
    real_delete = SessionDB.delete_session

    def spy(self, session_id, *a, **kw):
        deletes.append(session_id)
        return real_delete(self, session_id, *a, **kw)

    monkeypatch.setattr(SessionDB, "delete_session", spy)
    return sweeps, deletes


@pytest.fixture
def world(private_world, monkeypatch):
    holder = {**DEFAULTS, "mode": "private", "apply_to_platforms": ["cli"]}
    sweeps, deletes = _install(monkeypatch, private_world, holder)
    return SimpleNamespace(paths=private_world, settings=holder, sweeps=sweeps, deletes=deletes)


def armed_open(w, sid, platform="cli", started=None):
    """Ledger candidate via the real hook, swept to armed while its row is open."""
    make_db(w.paths, [(sid, platform, None, started or time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, platform, w.paths)
    lifecycle.run_sweep(w.paths)
    assert entry(w.paths, sid).get("state") == "armed"


# --- 1. HIGH: mode off mid-life -------------------------------------------------

def test_armed_chat_mode_off_midlife_is_never_deleted(world):
    p = world.paths
    armed_open(world, "mid-off")
    gen1 = gen(p)["n"]

    world.settings["mode"] = "off"
    lifecycle.observe_session_start("other-while-off", "cli", p)  # real path records the flip
    assert gen(p)["n"] == gen1 + 1 and gen(p)["armed"] is False
    close_row(p, "mid-off", "cli_close")

    world.settings["mode"] = "private"
    lifecycle.observe_session_start("other-after", "cli", p)
    assert gen(p)["n"] == gen1 + 2 and gen(p)["armed"] is True

    # positive control born in the NEW generation: the same sweep does delete it
    make_db(p, [("ctrl-gen3", "cli", None, time.time() + 5, None, None)])
    lifecycle.observe_session_start("ctrl-gen3", "cli", p)
    close_row(p, "ctrl-gen3", "cli_close")

    lifecycle.run_sweep(p)
    assert not present(p, "ctrl-gen3") and "ctrl-gen3" in world.deletes
    assert present(p, "mid-off"), "chat that lived through a mode-off period was deleted"
    assert "mid-off" not in world.deletes
    assert entry(p, "mid-off")["state"] == "excluded"  # permanently, never retried
    lifecycle.run_sweep(p)
    assert present(p, "mid-off")
    notice = lifecycle.finalize("mid-off", "cli", p) or ""
    assert "queued" not in notice and lifecycle.NOTICE_DELETED not in notice


def test_sweep_start_records_mode_change_then_never_deletes(world):
    p = world.paths
    armed_open(world, "sw-off")
    close_row(p, "sw-off", "cli_close")
    world.settings["mode"] = "off"
    lifecycle.run_sweep(p)
    g = gen(p)
    assert g["armed"] is False and g.get("mode") == "off", "sweep did not record the mode change"
    assert present(p, "sw-off")
    world.settings["mode"] = "private"
    lifecycle.run_sweep(p)  # records the re-enable generation, then sweeps
    assert gen(p)["armed"] is True
    assert present(p, "sw-off") and world.deletes == []
    assert entry(p, "sw-off")["state"] == "excluded"


def test_observed_off_marker_blocks_deletion_without_generation_change(world):
    p = world.paths
    armed_open(world, "obs-off")
    armed_open(world, "obs-ctrl")
    close_row(p, "obs-off", "cli_close")
    close_row(p, "obs-ctrl", "cli_close")
    mark_observed_off(p, "obs-off")
    n_before = gen(p)["n"]
    lifecycle.run_sweep(p)
    assert gen(p)["n"] == n_before  # no generation change involved
    assert present(p, "obs-off") and "obs-off" not in world.deletes
    assert entry(p, "obs-off")["state"] == "excluded"
    assert entry(p, "obs-off").get("observed_off") is True
    assert not present(p, "obs-ctrl")  # control: same sweep, unmarked chat deleted


@pytest.mark.parametrize("mark_child", [True, False])
def test_observed_off_on_delegate_child_blocks_whole_fence(world, mark_child):
    p = world.paths
    now = time.time()
    make_db(p, [("dparent", "cli", None, now + 1, None, None),
                ("dchild", "delegate", "dparent", now + 2, None, None, DELEGATE.format("dparent"))])
    lifecycle.observe_session_start("dparent", "cli", p)
    lifecycle.observe_subagent_start("dparent", "dchild", p)
    lifecycle.run_sweep(p)
    assert entry(p, "dparent")["state"] == "armed" and entry(p, "dchild")["state"] == "armed"
    if mark_child:
        mark_observed_off(p, "dchild")
    close_row(p, "dchild", "agent_close")
    close_row(p, "dparent", "cli_close")
    lifecycle.run_sweep(p)
    if mark_child:
        assert present(p, "dparent") and present(p, "dchild")
        assert world.deletes == []
        assert entry(p, "dparent")["state"] == "excluded"
    else:  # control: the delegate fence path really deletes
        assert not present(p, "dparent") and not present(p, "dchild")


def test_target_from_older_generation_refuses_fence(world):
    p = world.paths
    now = time.time()
    make_db(p, [("gparent", "cli", None, now + 1, None, None),
                ("gchild", "delegate", "gparent", now + 2, None, None, DELEGATE.format("gparent"))])
    lifecycle.observe_session_start("gparent", "cli", p)
    lifecycle.observe_subagent_start("gparent", "gchild", p)
    lifecycle.run_sweep(p)
    store.mutate("ledger", p.data_dir,
                 lambda l: {**l, H("gchild"): {**l[H("gchild")], "generation": gen(p)["n"] - 1}})
    close_row(p, "gchild", "agent_close")
    close_row(p, "gparent", "cli_close")
    lifecycle.run_sweep(p)
    assert present(p, "gparent") and present(p, "gchild") and world.deletes == []


LANDINGS = ["observed_off", "global_mode_off", "keep_override", "off_override", "keep_history",
            "consent_revoked", "out_of_scope", "generation_changed", "remove_history_off"]


@pytest.mark.parametrize("landing", LANDINGS)
def test_observation_landing_just_before_fenced_delete_holds_it_off(world, monkeypatch, landing):
    """The sweep snapshot says 'armed + closed'; a concurrent hook lands AFTER
    that snapshot and BEFORE the fence. The fence's own fresh, locked admission
    must see it (deterministic interleaving: the landing runs inside the call)."""
    p = world.paths
    sid = f"hold-{landing}".replace("_", "-")
    armed_open(world, sid)
    close_row(p, sid, "cli_close")

    def land():
        if landing == "observed_off":
            mark_observed_off(p, sid)
        elif landing == "global_mode_off":
            world.settings["mode"] = "off"  # no generation recorded yet
        elif landing == "keep_override":
            lifecycle.set_override(sid, "keep", p)
        elif landing == "off_override":
            lifecycle.set_override(sid, "off", p)
        elif landing == "keep_history":
            world.settings["keep_history"] = True
        elif landing == "consent_revoked":
            store.mutate("meta", p.data_dir,
                         lambda m: store.set_consent(m, recorded=False, now=time.time()))
        elif landing == "out_of_scope":
            world.settings["apply_to_platforms"] = []
        elif landing == "generation_changed":
            store.mutate("meta", p.data_dir,
                         lambda m: store.new_generation(m, armed=True, reason="setup", now=time.time()))
        elif landing == "remove_history_off":
            world.settings["remove_history_on_close"] = False

    real_fenced = lifecycle.fenced_delete
    reached: list[str] = []

    def racing(raw_id, key, ledger, paths):
        land()
        reached.append(raw_id)
        return real_fenced(raw_id, key, ledger, paths)

    monkeypatch.setattr(lifecycle, "fenced_delete", racing)
    lifecycle.run_sweep(p)
    assert reached == [sid], "the fence was never reached — test would be vacuous"
    assert present(p, sid), f"{landing} landing before the fence did not hold the delete off"
    assert world.deletes == []
    assert entry(p, sid).get("deleting") is None


def test_concurrent_off_marker_survives_the_arming_merge(world, monkeypatch):
    """Lost-update guard: an observed_off persisted between the sweep's
    validation and its merge must survive the merge and block deletion."""
    p = world.paths
    make_db(p, [("merge-race", "cli", None, time.time() + 1, time.time() + 40, "cli_close")])
    lifecycle.observe_session_start("merge-race", "cli", p)
    real_validate = lifecycle._validate_for_arming_from_ledger

    def validate_then_race(*a, **kw):
        res = real_validate(*a, **kw)
        mark_observed_off(p, "merge-race")
        return res

    monkeypatch.setattr(lifecycle, "_validate_for_arming_from_ledger", validate_then_race)
    lifecycle.run_sweep(p)
    assert present(p, "merge-race") and world.deletes == []
    assert entry(p, "merge-race").get("observed_off") is True


# --- 2. finalize notices from fresh state ---------------------------------------

def test_finalize_under_mode_off_is_not_queued_and_records_flip(world):
    p = world.paths
    armed_open(world, "fin-off")
    close_row(p, "fin-off", "cli_close")
    world.settings["mode"] = "off"
    notice = lifecycle.finalize("fin-off", "cli", p) or ""
    assert "queued" not in notice and lifecycle.NOTICE_DELETED not in notice
    assert gen(p)["armed"] is False  # finalize observed the flip (cheap meta RMW)
    world.settings["mode"] = "private"
    lifecycle.run_sweep(p)
    assert present(p, "fin-off") and world.deletes == []


def test_finalize_cli_new_boundary_is_not_queued(world):
    p = world.paths
    armed_open(world, "fin-new")
    notice = lifecycle.finalize("fin-new", "cli", p, reason="session_boundary") or ""
    assert "queued" not in notice
    close_row(p, "fin-new", "new_session")  # core ends the row AFTER finalize on /new
    lifecycle.run_sweep(p)
    assert present(p, "fin-new") and world.deletes == []
    # control: CLI quit (reason=shutdown) on an armed open chat IS queued, then removed
    armed_open(world, "fin-quit")
    notice = lifecycle.finalize("fin-quit", "cli", p, reason="shutdown") or ""
    assert lifecycle.NOTICE_QUEUED in notice
    close_row(p, "fin-quit", "cli_close")
    lifecycle.run_sweep(p)
    assert not present(p, "fin-quit")


def test_finalize_pre_setup_resume_is_not_queued(fake_paths, monkeypatch):
    p = fake_paths
    holder = {**DEFAULTS, "mode": "private", "apply_to_platforms": ["cli"]}
    sweeps, deletes = _install(monkeypatch, p, holder)
    old = time.time() - 5000
    make_db(p, [("pre-setup", "cli", None, old, None, None)])
    add_message(p, "pre-setup", old + 1)
    store.mutate("meta", p.data_dir, lambda m: store.new_generation(
        store.set_consent(m, recorded=True, now=time.time()), armed=True, reason="setup",
        now=time.time()))
    lifecycle.observe_session_start("pre-setup", "cli", p)  # resumed after setup
    for reason, closed in (("shutdown", False), ("shutdown", True)):
        if closed:
            close_row(p, "pre-setup", "cli_close")
        notice = lifecycle.finalize("pre-setup", "cli", p, reason=reason) or ""
        assert "queued" not in notice and lifecycle.NOTICE_DELETED not in notice
    lifecycle.run_sweep(p)
    assert present(p, "pre-setup") and deletes == []
    assert entry(p, "pre-setup")["state"] == "excluded"


def test_finalize_first_run_without_setup_gives_setup_hint(fake_paths, monkeypatch):
    p = fake_paths
    holder = {**DEFAULTS, "mode": "private", "apply_to_platforms": ["cli"]}
    sweeps, _deletes = _install(monkeypatch, p, holder)
    lifecycle.observe_session_start("first-run", "cli", p)
    notice = lifecycle.finalize("first-run", "cli", p) or ""
    assert "hermes incognito-private-mode setup" in notice
    assert "queued" not in notice and lifecycle.NOTICE_DELETED not in notice
    # never observed at start: still the hint, not silence
    assert "hermes incognito-private-mode setup" in (lifecycle.finalize("unseen-1", "cli", p) or "")
    # mode off / out of scope: no hint (setup would not remove these anyway)
    assert lifecycle.finalize("first-run", "telegram", p) is None
    holder["mode"] = "off"
    assert "setup" not in (lifecycle.finalize("first-run", "cli", p) or "")


def test_finalize_with_consent_gives_no_setup_hint(world):
    armed_open(world, "with-consent")
    notice = lifecycle.finalize("with-consent", "cli", world.paths, reason="shutdown") or ""
    assert "setup" not in notice and lifecycle.NOTICE_QUEUED in notice


def test_finalize_never_sweeps_synchronously(world, monkeypatch):
    armed_open(world, "fin-async")
    close_row(world.paths, "fin-async", "cli_close")

    def boom(*_a, **_kw):
        raise AssertionError("finalize ran a sweep inline")

    monkeypatch.setattr(lifecycle, "run_sweep", boom)
    before = list(world.sweeps)
    lifecycle.finalize("fin-async", "cli", world.paths)
    assert world.sweeps[len(before):] == ["finalize"]
    assert present(world.paths, "fin-async")


# --- 3. lineage disclosure -------------------------------------------------------

def test_remaining_lineage_multilevel_compression_and_delegates(world):
    """root -(compression)-> mid -(compression)-> tip; tip has a delegate child
    (deleted with tip), root has an OPEN delegate child (survives), mid has a
    ledgered delegate whose row is known ABSENT (must not count)."""
    p = world.paths
    now = time.time()
    make_db(p, [
        ("root", "cli", None, now + 1, now + 10, "compression"),
        ("mid", "cli", "root", now + 11, now + 20, "compression"),
        ("tip", "cli", "mid", now + 21, None, None),
        ("tchild", "delegate", "tip", now + 22, None, None, DELEGATE.format("tip")),
        ("rchild", "delegate", "root", now + 3, None, None, DELEGATE.format("root")),
    ])
    lifecycle.observe_session_start("root", "cli", p)
    lifecycle.observe_subagent_start("root", "rchild", p)
    lifecycle.observe_post_compaction("mid", "root", p)
    lifecycle.observe_subagent_start("mid", "gonechild", p)  # never written to the DB
    lifecycle.observe_post_compaction("tip", "mid", p)
    lifecycle.observe_subagent_start("tip", "tchild", p)
    close_row(p, "tchild", "agent_close")
    close_row(p, "tip", "cli_close")
    lifecycle.run_sweep(p)
    assert not present(p, "tip") and not present(p, "tchild")
    assert present(p, "root") and present(p, "mid") and present(p, "rchild")

    ledger = store.read("ledger", p.data_dir)
    parts = lifecycle._remaining_lineage_parts(ledger, "tip", p)
    assert parts == {"earlier": 2, "earlier_unchecked": 0, "other": 1, "other_unchecked": 0}, parts
    notice = lifecycle.finalize("tip", "cli", p)
    assert notice is not None and lifecycle.NOTICE_DELETED in notice
    assert "2 earlier part(s) of this chat remain" in notice
    assert "1 other tracked part(s) of this chat remain" in notice
    assert "compress" not in notice.lower()  # no hardcoded explanation
    assert not hasattr(lifecycle, "_remaining_lineage_members_legacy")


def test_remaining_lineage_unreadable_db_is_unchecked_not_absent(world):
    p = world.paths
    store.mutate("ledger", p.data_dir, lambda l: {
        **l,
        H("par-x"): {"state": "armed", "raw_id": "par-x", "children": [H("tip-x")]},
        H("tip-x"): {"state": "deleted", "deleted_how": "fenced", "parent_hash": H("par-x")},
    })
    assert not p.db_path.exists()
    parts = lifecycle._remaining_lineage_parts(store.read("ledger", p.data_dir), "tip-x", p)
    assert parts == {"earlier": 0, "earlier_unchecked": 1, "other": 0, "other_unchecked": 0}
    notice = lifecycle.finalize("tip-x", "cli", p) or ""
    assert "1 earlier part(s) of this chat remain or could not be checked" in notice


# --- 4. absent vs error -----------------------------------------------------------

def test_read_row_checked_missing_db_and_invalid_ids_are_errors(world, tmp_path):
    missing = tmp_path / "missing" / "state.db"
    assert lifecycle.read_row_checked(missing, "abc") == ("error", None)
    assert not missing.exists()  # a read never creates the DB
    a_dir = tmp_path / "dir.db"
    a_dir.mkdir()
    assert lifecycle.read_row_checked(a_dir, "abc")[0] == "error"
    make_db(world.paths, [("real-1", "cli", None, time.time() + 1, None, None)])
    assert lifecycle.read_row_checked(world.paths.db_path, "real-1")[0] == "present"
    assert lifecycle.read_row_checked(world.paths.db_path, "nope-1")[0] == "absent"
    for bad in ("", "-lead", "a b", "real-1\n", "x" * 201, "a/b", None, 7):
        assert lifecycle.read_row_checked(world.paths.db_path, bad) == ("error", None), repr(bad)


def test_missing_db_never_tombstones_armed_entry(world):
    p = world.paths
    armed_open(world, "db-gone")
    close_row(p, "db-gone", "cli_close")
    moved = []
    for suffix in ("", "-wal", "-shm"):
        src = p.db_path.with_name(p.db_path.name + suffix)
        if src.exists():
            dst = src.with_name(src.name + ".moved")
            os.replace(src, dst)
            moved.append((dst, src))
    lifecycle.run_sweep(p)
    lifecycle.on_external_delete("db-gone", "delete", p)
    assert entry(p, "db-gone")["state"] == "armed", "missing DB was treated as a deleted row"
    for dst, src in moved:
        os.replace(dst, src)
    lifecycle.run_sweep(p)  # control: DB back -> the fenced delete runs
    assert not present(p, "db-gone")
    assert entry(p, "db-gone")["deleted_how"] == "fenced"


def test_unreadable_meta_is_transient_not_a_mass_tombstone(world):
    """No generation readable (corrupt meta) proves nothing about a chat:
    refuse now, never permanently exclude; deletion resumes once readable."""
    p = world.paths
    armed_open(world, "meta-gone")
    close_row(p, "meta-gone", "cli_close")
    meta_file = p.data_dir / "meta.json"
    saved = meta_file.read_bytes()
    meta_file.write_text("{not json")
    lifecycle.run_sweep(p)
    assert present(p, "meta-gone") and world.deletes == []
    assert entry(p, "meta-gone")["state"] == "armed"
    meta_file.write_bytes(saved)
    lifecycle.run_sweep(p)  # control
    assert not present(p, "meta-gone")


# --- 5. unproven explicit close reasons ------------------------------------------

def test_unproven_close_reasons_are_not_deletion_triggers():
    assert {"new_session", "resumed_other", "branched"}.isdisjoint(lifecycle.ALLOWED_END_REASONS)
    assert {"new_session", "resumed_other", "branched"} <= lifecycle.EXPLICIT_UNPROVEN_END_REASONS
    assert lifecycle.AUTOMATIC_END_REASONS.isdisjoint(lifecycle.ALLOWED_END_REASONS)
    for reason in ("new_session", "resumed_other", "branched", "ws_disconnect", "compression"):
        assert not lifecycle.row_is_closed({"ended_at": 1.0, "end_reason": reason})


@pytest.mark.parametrize("reason,deleted", [
    ("cli_close", True), ("tui_close", True), ("agent_close", True),
    ("new_session", False), ("resumed_other", False), ("branched", False),
    ("ws_disconnect", False), ("idle_timeout", False), ("compression", False), ("weird", False),
])
def test_end_reason_gate_on_real_rows(world, reason, deleted):
    sid = f"er-{reason}".replace("_", "-")
    armed_open(world, sid)
    close_row(world.paths, sid, reason)
    lifecycle.run_sweep(world.paths)
    assert present(world.paths, sid) is (not deleted)


def test_unadmitted_closes_never_starve_the_batch(world):
    p = world.paths
    for i, reason in enumerate(("new_session", "branched", "resumed_other")):
        armed_open(world, f"stuck-{i}")
        close_row(p, f"stuck-{i}", reason)
    armed_open(world, "after-stuck")
    close_row(p, "after-stuck", "cli_close")
    lifecycle.run_sweep(p, batch_limit=2)
    assert not present(p, "after-stuck")
    assert all(present(p, f"stuck-{i}") for i in range(3))
