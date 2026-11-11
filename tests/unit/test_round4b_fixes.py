"""Round-4b fixes (Opus reviewers): regressions for each finding.

F9 reload-deadlock hint, F7 resume transcript fence, F1-A cleanup outside hold,
F2-B MoA block, F3-B BOM persona, F6-A FailedConfigRead, F5-A bedrock shape,
F3-A user-residue stickiness, F9-B keep reply honesty, F11-B propagation.
"""
import json
import sqlite3
import threading
import time

import pytest

from ipm import commands, enforce, lifecycle, settings as settings_mod, store, strip, synthetic


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


def _close(paths, sid, reason="cli_close"):
    conn = sqlite3.connect(str(paths.db_path))
    conn.execute("UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                 (time.time(), reason, sid))
    conn.commit()
    conn.close()


# --- F9: the invalidation hint must not block behind the fence ------------------

def test_external_delete_hint_does_not_block_on_busy_lock(world):
    """Round-4 Opus F9: a plugin force-reload gives the new module copy an
    empty reentrancy registry; a blocking mutate from the hook core dispatches
    INSIDE our own delete would self-deadlock the fence thread. The hint is
    therefore non-blocking: with the ledger lock held elsewhere it returns
    promptly and changes nothing."""
    paths, _settings = world
    from ipm.store import hash_session_id
    sid = "f9-reload"
    _make_db(paths, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", paths)
    key = hash_session_id(sid)
    assert store.read("ledger", paths.data_dir).get(key)

    release = threading.Event()
    acquired = threading.Event()

    def holder():
        with store.store_lock(paths.data_dir):
            acquired.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert acquired.wait(5)

    done = threading.Event()

    def hint():
        lifecycle.on_external_delete(sid, "external", paths)
        done.set()

    t2 = threading.Thread(target=hint, daemon=True)
    t2.start()
    assert done.wait(5), "invalidation hint blocked behind the fence lock"
    release.set()
    t.join(10)
    # skipped, not applied — residue recovery re-verifies later
    assert store.read("ledger", paths.data_dir).get(key, {}).get("state") != "deleted"


# --- F7: a resume landing between admission and delete is fenced ---------------

def test_resume_between_admission_and_delete_refuses(world, monkeypatch):
    """The fence captures each target's display snapshot at admission and core
    re-checks it inside its own write transaction: a /resume plus a new message
    landing in the window aborts the deletion."""
    from hermes_state import SessionDB
    p, _settings = world
    sid = "f7-resume"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    _close(p, sid)

    real_delete = SessionDB.delete_session

    def sneak_resume_then_delete(self, *a, **k):
        # simulate /resume + a new message arriving in the window: reopen and
        # append a message BEFORE the delete transaction runs
        conn = sqlite3.connect(str(p.db_path))
        conn.execute("UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?", (sid,))
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                     (sid, "user", "resumed and continuing", time.time()))
        conn.commit()
        conn.close()
        return real_delete(self, *a, **k)

    monkeypatch.setattr(SessionDB, "delete_session", sneak_resume_then_delete)
    from ipm.store import hash_session_id
    rc = lifecycle.fenced_delete(sid, hash_session_id(sid), None, p)
    assert rc == "refused"  # transcript fence held
    assert lifecycle.read_row_checked(p.db_path, sid)[0] == "present"


def test_transcript_fence_does_not_block_clean_deletes(world):
    """Positive control: without tampering, the snapshot fence deletes normally."""
    p, _settings = world
    sid = "f7-clean"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    _close(p, sid)
    from ipm.store import hash_session_id
    rc = lifecycle.fenced_delete(sid, hash_session_id(sid), None, p)
    assert rc == "deleted"
    assert lifecycle.read_row_checked(p.db_path, sid)[0] == "absent"


# --- F2-B: MoA prepared request is unenforced — block ---------------------------

def test_moa_prepared_request_blocks(world):
    payload = {"messages": [{"role": "system", "content": "clean"},
                            {"role": "user", "content": "hi"}],
               "model": "m",
               "_moa_prepared_request": {"messages": [{"role": "system",
                                                       "content": "MEMORY BLOCK CONTENT"}]}}
    result, seen = [], []
    out = enforce.on_llm_execution(
        request=payload, session_id="f2b", platform="cli", provider="moa",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m",
        next_call=lambda req: seen.append(req) or "provider-answer")
    assert not seen  # never reaches the provider
    assert out is not None and "provider-answer" != out


# --- F3-B: BOM-led SOUL.md persona is detected ----------------------------------

def test_persona_text_strips_bom_and_legacy(world):
    paths, _settings = world
    paths.home.mkdir(parents=True, exist_ok=True)
    soul = paths.home / "SOUL.md"
    soul.write_bytes("\ufeffIdentity: moonlight priestess\nBody line\n".encode("utf-8"))
    text = enforce._persona_text(paths)
    assert text is not None and not text.startswith("\ufeff")
    assert "Identity" in text


# --- F6-A: FailedConfigRead behaves like a read failure --------------------------

def test_failed_config_read_fails_toward_no_deletion(monkeypatch, tmp_path):
    from hermes_cli.config_read_errors import FailedConfigRead
    monkeypatch.setattr(settings_mod, "capture_paths", lambda: type("P", (), {"home": tmp_path})())
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: FailedConfigRead({"model": "x"}, read_error="broken yaml"))
    monkeypatch.setattr(settings_mod, "_config_cache", {"mtime": None, "settings": None})
    s = settings_mod.read_settings()
    assert s["remove_history_on_close"] is False
    assert s["apply_to_platforms"] == []
    assert s["allow_unverified"] is False
    assert s["strip_persona"] is True


# --- F5-A: bedrock_converse gets a real terminal block ---------------------------

def test_bedrock_converse_block_is_chat_shaped():
    # round-#5 Opus: core's BedrockTransport.validate_response requires a raw
    # Converse dict with an 'output' key (a ChatCompletion dict is rejected)
    from agent.transports.bedrock import BedrockTransport
    shaped = synthetic.block_response("bedrock_converse", "Blocked.")
    assert isinstance(shaped, dict) and "output" in shaped
    assert BedrockTransport().validate_response(shaped) is True
    dict_shape = synthetic.block_response_dict("bedrock_converse", "Blocked.")
    assert BedrockTransport().validate_response(dict_shape) is True
    assert dict_shape["output"]["message"]["content"][0]["text"] == "Blocked."


def test_bedrock_converse_positive_block_does_not_scrub(world):
    p, settings = world
    settings["allow_unverified"] = False
    seen = []
    payload = {"messages": [{"role": "user", "content": "SECRET-BEDROCK"}], "model": "m"}
    out = enforce.on_llm_execution(
        request=payload, session_id="f5a", platform="cli", provider="bedrock",
        base_url="https://attacker.example/v1", api_mode="bedrock_converse", model="m",
        next_call=lambda req: seen.append(req) or "provider-answer")
    assert not seen  # synthetic block, not the scrub path
    assert "provider-answer" not in str(out)


# --- F3-A: quoted memory block in history must not wedge the session -------------

def _render(body):
    from tools.memory_tool_store import MemoryStore
    return MemoryStore()._render_block("memory", [body])


def test_old_user_turn_quoting_memory_does_not_block(world):
    quoted = 'MEMORY (your personal notes) [9% \u2014 30/2,200 chars]\nnothing secret here'
    payload = {"messages": [
        {"role": "system", "content": "clean system"},
        {"role": "user", "content": quoted},          # old turn: quoted residue
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "fresh question"},  # last turn: clean
    ], "model": "m"}
    result, seen = [], []
    out = enforce.on_llm_execution(
        request=payload, session_id="f3a", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m",
        next_call=lambda req: seen.append(req) or "provider-answer")
    assert out == "provider-answer" and len(seen) == 1  # quoted history is not a leak


def test_last_user_turn_residue_still_blocks(world):
    payload = {"messages": [
        {"role": "system", "content": "clean system"},
        {"role": "user", "content": _render("private body")},
    ], "model": "m"}
    seen = []
    enforce.on_llm_execution(
        request=payload, session_id="f3a2", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m",
        next_call=lambda req: seen.append(req) or "provider-answer")
    assert not seen  # live memory in the CURRENT turn still blocks


# --- F9-B: /private keep on an already-deleted chat answers honestly -------------

def test_keep_reply_on_deleted_chat(world, monkeypatch, capsys):
    p, _settings = world
    from ipm.store import hash_session_id
    sid = "f9b"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    _close(p, sid)
    from ipm.store import hash_session_id as h
    lifecycle.fenced_delete(sid, h(sid), None, p)
    monkeypatch.setattr(commands, "capture_paths", lambda: p)
    reply = commands.handle_private_command("keep", session_id=sid, platform="cli")
    assert "already removed" in reply
