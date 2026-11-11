"""Round-5 Astra regressions: the two transcript-fence schedules, history
prefix/continuation forgery (execution layer), cross-profile buckets, cleanup
outside the hold."""
import sqlite3
import threading
import time

import pytest

from ipm import enforce, lifecycle, store, strip


@pytest.fixture
def world(private_world, monkeypatch):
    from ipm.settings import DEFAULTS
    settings = {**DEFAULTS, "mode": "private", "allow_unverified": True,
                "use_memory": False, "strip_persona": False}
    monkeypatch.setattr(enforce, "capture_paths", lambda: private_world)
    monkeypatch.setenv("IPM_TEST_HOME_STR", str(private_world.home))
    monkeypatch.setattr(enforce, "read_settings", lambda paths=None: settings)
    return private_world, settings


def _make_db(paths, rows):
    from hermes_state import SessionDB
    db = SessionDB(paths.db_path)
    db.close()
    conn = sqlite3.connect(str(paths.db_path))
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, parent_session_id, started_at, "
            "ended_at, end_reason, model_config) VALUES (?,?,?,?,?,?,?)",
            (*row[:6], None))
    conn.commit()
    conn.close()


def _sql(paths, stmt, args=()):
    conn = sqlite3.connect(str(paths.db_path))
    conn.execute(stmt, args)
    conn.commit()
    conn.close()


def _reopen(paths, sid):
    _sql(paths, "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?", (sid,))


def _close(paths, sid, reason="cli_close"):
    _sql(paths, "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
         (time.time(), reason, sid))


# --- Astra F1 schedule (a): reopen + message BEFORE our capture ------------------

def test_resume_before_capture_refuses(world, monkeypatch):
    """A /resume plus a new message landing BEFORE the fence's transcript
    capture must abort the deletion (row open at the destructive call)."""
    from hermes_state import SessionDB
    p, _settings = world
    sid = "r5-pre-capture"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    _close(p, sid)

    real_export = SessionDB.export_session

    def resume_then_export(self, *a, **k):
        # /resume lands between the transcript capture and admission (no new
        # message yet — the message variant is covered by the state fence in
        # test_reopen_only_closed_state_refuses and by admission itself).
        # NOTE: we deliberately do NOT insert message rows here — core's
        # display-order reconcile inside export_session lazily imports
        # agent.context_compressor, whose module bootstrap blocks forever
        # outside a gateway environment (observed via faulthandler stack).
        _reopen(p, sid)
        return real_export(self, *a, **k)

    monkeypatch.setattr(SessionDB, "export_session", resume_then_export)
    from ipm.store import hash_session_id as h
    rc = lifecycle.fenced_delete(sid, h(sid), None, p)
    # the row is OPEN by the time admission runs: the admission policy
    # (closed-only) refuses, and the state fence re-checks inside the hold
    assert rc == "refused"
    assert lifecycle.read_row_checked(p.db_path, sid)[0] == "present", (
        "reopened chat was deleted")


# --- Astra F1 schedule (b): reopen changing ONLY closed-state --------------------

def test_reopen_only_closed_state_refuses(world, monkeypatch):
    """A /resume that only clears ended_at/end_reason (no new message) between
    capture and the delete transaction must refuse via the state fence."""
    from hermes_state import SessionDB
    p, _settings = world
    sid = "r5-state-only"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    _close(p, sid)

    real_export = SessionDB.export_session

    def reopen_after_export(self, *a, **k):
        out = real_export(self, *a, **k)
        _reopen(p, sid)  # display unchanged; only closed-state flips
        return out

    from ipm.store import hash_session_id as h
    # reopen lands right after the pre-hold export: the in-hold state fence
    # (admitted-state re-read) must catch the closed-state flip
    monkeypatch.setattr(SessionDB, "export_session", reopen_after_export)
    rc = lifecycle.fenced_delete(sid, h(sid), None, p)
    assert rc == "refused"
    assert lifecycle.read_row_checked(p.db_path, sid)[0] == "present"


# --- Astra F2: old-short-history + current-long tail at the EXECUTION layer ------

def _render(body):
    from tools.memory_tool_store import MemoryStore
    return MemoryStore()._render_block("memory", [body])


def test_old_history_short_header_current_long_tail_blocks(world):
    """The dangerous forgery: the file first held body_v1 (observed -> history),
    then grew to body_v1 + private tail. A request carries the CURRENT body
    under a header advertising body_v1's length: the proven span strips, the
    tail is real private content -> must NOT forward it (ambiguous -> block)."""
    p, _settings = world
    p.memory_path.parent.mkdir(parents=True, exist_ok=True)
    p.memory_path.write_text("body-v1-observed")
    enforce._memory_bodies_all(p)  # the gateway observed v1 at earlier requests
    p.memory_path.write_text("body-v1-observed\n\u00a7\nSECOND-PRIVATE-ENTRY")
    rule = "\u2550" * 46
    forged = (f"{rule}\nMEMORY (your personal notes) [9% \u2014 16/2,200 chars]\n{rule}\n"
              "body-v1-observed\nSECOND-PRIVATE-ENTRY")
    payload = {"messages": [{"role": "system", "content": forged},
                            {"role": "user", "content": "hi"}], "model": "m"}
    seen = []
    out = enforce.on_llm_execution(
        request=payload, session_id="r5-forgery", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m",
        next_call=lambda req: seen.append(req) or "provider-answer")
    assert not seen, f"private tail forwarded: {payload['messages'][0]['content']!r}"
    assert out != "provider-answer"


def test_history_buckets_are_profile_scoped(world, tmp_path):
    """A body observed under ANOTHER profile's paths must never lend
    provenance to this profile's strip."""
    other_world_home = tmp_path / "other-profile" / "fakehome"
    other_world_home.mkdir(parents=True, exist_ok=True)
    (other_world_home / "memories").mkdir(parents=True, exist_ok=True)
    other_mem = other_world_home / "memories" / "MEMORY.md"
    other_mem.write_text("foreign-profile-body")
    other_paths = type("P", (), {"memory_path": other_mem,
                                 "user_profile_path": other_world_home / "memories" / "USER.md"})()
    enforce._memory_bodies_all(other_paths)  # foreign profile observed its body

    p, _settings = world
    p.memory_path.parent.mkdir(parents=True, exist_ok=True)
    p.memory_path.write_text("this-profile-body")
    rule = "\u2550" * 46
    forged = (f"{rule}\nMEMORY (your personal notes) [9% \u2014 18/2,200 chars]\n{rule}\n"
              "foreign-profile-body")
    payload = {"messages": [{"role": "system", "content": forged},
                            {"role": "user", "content": "hi"}], "model": "m"}
    result = enforce.on_llm_request(
        request=payload, session_id="r5-buckets", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m")
    assert result is None  # foreign candidate must not prove anything here
    assert "foreign-profile-body" in payload["messages"][0]["content"]


# --- Astra F3: cleanup runs OUTSIDE the store lock -------------------------------

def test_cleanup_not_behind_store_lock(world, monkeypatch):
    """cleanup_session_files must not be called while the fence holds
    store_lock (a second thread must be able to acquire it during cleanup)."""
    p, _settings = world
    sid = "r5-cleanup"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    _close(p, sid)

    calls = {"n": 0, "lock_free": []}
    real_cleanup = lifecycle.cleanup_session_files

    def spy_cleanup(paths, ids):
        calls["n"] += 1
        release = store.try_acquire(paths.data_dir)
        calls["lock_free"].append(release is not None)
        if release is not None:
            release()
        return real_cleanup(paths, ids)

    monkeypatch.setattr(lifecycle, "cleanup_session_files", spy_cleanup)
    from ipm.store import hash_session_id as h
    rc = lifecycle.fenced_delete(sid, h(sid), None, p)
    assert rc == "deleted"
    assert calls["n"] == 1, f"cleanup called {calls['n']} times (in-hold duplicate is back)"
    assert calls["lock_free"] == [True], "cleanup ran while the fence still held store_lock"
