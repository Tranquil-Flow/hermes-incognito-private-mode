"""Round-5 Opus regressions: per-target transcript fencing, archived admission,
transient settings failures never cached, core-validated block shapes, vacuous
test repairs (FailedConfigRead real signature, F11-B propagation)."""
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from ipm import enforce, lifecycle, settings as settings_mod, store, strip, synthetic


@pytest.fixture
def world(private_world, monkeypatch):
    from ipm.settings import DEFAULTS
    settings = {**DEFAULTS, "mode": "private", "allow_unverified": True,
                "use_memory": False, "strip_persona": False}
    monkeypatch.setattr(enforce, "capture_paths", lambda: private_world)
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


def _close(paths, sid, reason="cli_close"):
    _sql(paths, "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
         (time.time(), reason, sid))


# --- HIGH: transcript fence covers EVERY target (delegate child) -----------------

def test_fence_covers_delegate_child_transcripts(world, monkeypatch):
    """A message written into a NON-ROOT delete target after admission must
    abort the deletion (the fence snapshots every target, not just the root)."""
    from hermes_state import SessionDB
    p, _settings = world
    root, child = "r5root", "r5child"
    _make_db(p, [(root, "cli", None, time.time() + 1, None, None),
                 (child, "cli", root, time.time() + 2, None, None)])
    lifecycle.observe_session_start(root, "cli", p)
    lifecycle.observe_session_start(child, "cli", p)  # child must be a TARGET
    lifecycle.run_sweep(p)
    _close(p, root)
    _close(p, child)

    real_delete = SessionDB.delete_session

    def sneak_into_child_then_delete(self, *a, **k):
        conn = sqlite3.connect(str(p.db_path))
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                     (child, "user", "written into the child after admission", time.time()))
        conn.commit()
        conn.close()
        return real_delete(self, *a, **k)

    monkeypatch.setattr(SessionDB, "delete_session", sneak_into_child_then_delete)
    # core's lineage walk: a real delegate run returns BOTH ids (reviewer P1
    # observed ['fchild','fparent']). Simulate the multi-target set — the bug
    # was the plugin fencing only the ROOT's transcript.
    monkeypatch.setattr(SessionDB, "get_session_delete_targets",
                        lambda self, sid: [root, child], raising=False)
    from ipm.store import hash_session_id as h
    rc = lifecycle.fenced_delete(root, h(root), None, p)
    assert rc == "refused"
    assert lifecycle.read_row_checked(p.db_path, child)[0] == "present", (
        "delegate child's new message was deleted (root-only fencing)")


# --- MED: archived rows are refused at admission, hint or no hint -----------------

def test_archived_row_refused_at_admission(world):
    """The archived flag is read DIRECTLY from the DB at admission: even if the
    on_session_delete hint was dropped under lock contention, an archived chat
    is never deleted."""
    p, _settings = world
    sid = "r5archived"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    _close(p, sid)
    _sql(p, "UPDATE sessions SET archived = 1 WHERE id = ?", (sid,))
    from ipm.store import hash_session_id as h
    rc = lifecycle.fenced_delete(sid, h(sid), None, p)
    assert rc == "refused"
    assert lifecycle.read_row_checked(p.db_path, sid)[0] == "present"


# --- MED: transient settings failures are never cached ---------------------------

def test_transient_settings_failure_not_cached(monkeypatch, tmp_path):
    """One failed config read applies the fail-safe settings for THAT call but
    must not stick: the next read re-probes and recovers."""
    from hermes_cli.config_read_errors import FailedConfigRead
    home = tmp_path / "fakehome"
    home.mkdir(parents=True, exist_ok=True)
    paths = type("P", (), {"home": home})()
    monkeypatch.setattr(settings_mod, "capture_paths", lambda: paths)
    state = {"fail": True}

    def flaky_loader():
        if state["fail"]:
            return FailedConfigRead({"model": {"provider": "x"}}, error=OSError(24, "EMFILE"))
        return {"plugins": {"entries": {"incognito-private-mode": {"settings": {
            "remove_history_on_close": True, "keep_history": False}}}}}

    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config_readonly", flaky_loader)
    monkeypatch.setattr(settings_mod, "_config_cache", {"mtime": None, "settings": None})

    s1 = settings_mod.read_settings(paths)
    assert s1["remove_history_on_close"] is False  # fail-safe for the failed read
    state["fail"] = False
    s2 = settings_mod.read_settings(paths)  # recovered -> NOT the cached failure
    assert s2["remove_history_on_close"] is True, "failed read was cached and stuck"


# --- MED: block shapes pass core's transport validators ---------------------------

def test_block_shapes_pass_core_validators():
    from agent.transports.bedrock import BedrockTransport
    from agent.transports.chat_completions import ChatCompletionsTransport
    text = "Blocked by private mode."
    conv = synthetic.block_response_dict("bedrock_converse", text)
    assert BedrockTransport().validate_response(conv) is True
    chat = synthetic.block_response_dict("chat_completions", text)
    assert ChatCompletionsTransport().validate_response(chat) is True
    # bedrock via the shaped entrypoint too
    shaped = synthetic.block_response("bedrock_converse", text)
    assert BedrockTransport().validate_response(shaped) is True


# --- LOW repaired: FailedConfigRead with the REAL signature -----------------------

def test_failed_config_read_real_signature(monkeypatch, tmp_path):
    """The real constructor is FailedConfigRead(data, *, error=...). The old
    test's wrong kwarg made it vacuous (TypeError took the generic path)."""
    from hermes_cli.config_read_errors import FailedConfigRead
    home = tmp_path / "fakehome"
    home.mkdir(parents=True, exist_ok=True)
    paths = type("P", (), {"home": home})()
    monkeypatch.setattr(settings_mod, "capture_paths", lambda: paths)
    monkeypatch.setattr(settings_mod, "_config_cache", {"mtime": None, "settings": None})
    called = {"n": 0}

    def loader():
        called["n"] += 1
        return FailedConfigRead({"model": {"provider": "x"}}, error=ValueError("broken yaml"))

    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config_readonly", loader)
    s = settings_mod.read_settings(paths)
    assert called["n"] == 1  # the loader RETURNED the FailedConfigRead (not a raise)
    assert s["remove_history_on_close"] is False
    assert s["apply_to_platforms"] == []
    assert s["allow_unverified"] is False
    assert s["strip_persona"] is True


# --- LOW repaired: F11-B propagation is actually exercised ------------------------

def test_residue_scan_error_fails_closed(world, monkeypatch):
    """An exception inside the residue scan must reach the fail-closed outer
    handler (block), not vanish into a swallowed False."""
    p, settings = world
    payload = {"messages": [{"role": "user", "content": "hi"}], "model": "m"}
    monkeypatch.setattr(strip, "memory_title_residue",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("scanner boom")))
    seen = []
    out = enforce.on_llm_execution(
        request=payload, session_id="r5-f11", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m",
        next_call=lambda req: seen.append(req) or "provider-answer")
    assert not seen  # fail closed, never passthrough
    assert out != "provider-answer"
