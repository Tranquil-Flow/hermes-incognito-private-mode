"""Round-4c test gaps (Opus reviewer B): production observed_off write,
enforce-layer profile scoping, synthetic dict fallback, history availability."""
import sqlite3
import time

import pytest

from ipm import enforce, lifecycle, store, strip, synthetic


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


def _send(world, payload, sid, **overrides):
    seen = []
    kwargs = dict(session_id=sid, platform="cli", provider="custom",
                  base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m")
    kwargs.update(overrides)
    out = enforce.on_llm_execution(
        request=payload,
        next_call=lambda req: seen.append(req) or "provider-answer", **kwargs)
    return out, seen


# --- production observed_off write is exercised through the real hook ----------

def test_observed_off_written_by_production_request_hook(world):
    """The round-3 fix writes observed_off from enforce.on_llm_request itself
    (mode off observed on an armed entry). Drive the PRODUCTION hook — not a
    hand-copied helper — then verify the chat can never be deleted."""
    p, settings = world
    sid = "prod-off"
    _make_db(p, [(sid, "cli", None, time.time() + 1, None, None)])
    lifecycle.observe_session_start(sid, "cli", p)
    lifecycle.run_sweep(p)
    from ipm.store import hash_session_id
    assert store.read("ledger", p.data_dir).get(hash_session_id(sid), {}).get("state") == "armed"

    settings["mode"] = "off"  # the chat runs unprotected
    payload = {"messages": [{"role": "user", "content": "hi"}], "model": "m"}
    result = enforce.on_llm_request(
        request=payload, session_id=sid, platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m")
    assert result is None  # mode off: nothing applied
    entry = store.read("ledger", p.data_dir).get(hash_session_id(sid), {})
    assert entry.get("observed_off") is True, "production hook did not persist observed_off"

    _close(p, sid)
    lifecycle.run_sweep(p)
    assert lifecycle.read_row_checked(p.db_path, sid)[0] == "present"
    assert store.read("ledger", p.data_dir).get(hash_session_id(sid), {}).get("state") == "excluded"


# --- profile scoping exercised at the ENFORCEMENT layer -------------------------

def test_legacy_unscoped_confirmation_does_not_promote_enforcement(world):
    """A confirmation record without a profile (pre-3.7 format) must not
    promote the label on_llm_execution uses: strict mode blocks."""
    p, settings = world
    settings["allow_unverified"] = False

    def seed_legacy(c):
        c["legacy-anthropic"] = {"row_id": "anthropic-oauth", "host": "api.anthropic.com",
                                 "hosts": ["api.anthropic.com"], "auth": "oauth",
                                 "policy_rev": "r3", "provider": "anthropic"}
        return c
    store.mutate("confirmations", p.data_dir, seed_legacy)

    payload = {"messages": [{"role": "user", "content": "hi"}], "model": "c"}
    out, seen = _send(world, payload, "prof-legacy",
                      base_url="https://api.anthropic.com/v1")
    assert not seen  # unscoped legacy record must NOT promote past strict mode

    def seed_current(c):
        c["scoped-anthropic"] = {"row_id": "anthropic-oauth", "host": "api.anthropic.com",
                                 "hosts": ["api.anthropic.com"], "auth": "oauth",
                                 "policy_rev": enforce.routes_mod.load_table().get("policy_rev"),
                                 "provider": "anthropic",
                                 "profile": enforce.routes_mod.profile_scope(p.home)}
        return c
    store.mutate("confirmations", p.data_dir, seed_current)
    payload2 = {"messages": [{"role": "user", "content": "hi"}], "model": "c"}
    out2, seen2 = _send(world, payload2, "prof-scoped",
                        base_url="https://api.anthropic.com/v1")
    assert out2 == "provider-answer" and len(seen2) == 1  # correctly scoped: confirmed


# --- synthetic dict fallback is exercised ----------------------------------------

def test_dict_fallback_when_sdk_shaping_fails(world):
    """Round-#3 Astra F5 had no test (mutant M25 survived): when the SDK-shaped
    block construction fails, the dict fallback must carry the block and
    next_call must never run."""
    p, settings = world
    settings["allow_unverified"] = False
    seen = []
    payload = {"messages": [{"role": "user", "content": "hi"}], "model": "m"}
    original = synthetic.block_response

    def failing_shape(api_mode, text):
        if api_mode == "chat_completions":
            raise RuntimeError("SDK import failed")
        return original(api_mode, text)

    import ipm.enforce as _e
    monkey = pytest.MonkeyPatch()
    monkey.setattr(_e.synthetic, "block_response", failing_shape)
    try:
        out, _ = _send(world, payload, "dictfallback")
    finally:
        monkey.undo()
    assert not seen  # dict fallback delivered the block; provider untouched
    assert out is not None and out != "provider-answer"


# --- history availability end-to-end ---------------------------------------------

def test_history_enables_midsession_save_strip(world):
    """Availability (round-#4 Opus F2): save an entry mid-session; the OLD
    snapshot must still strip via the body history — while a brand-new entry
    stays in the (rewritten) request, and the CURRENT file's bodies are tried
    first."""
    p, _settings = world
    p.memory_path.parent.mkdir(parents=True, exist_ok=True)
    p.memory_path.write_text("old-secret-entry")
    from tools.memory_tool_store import MemoryStore
    old_snapshot = MemoryStore()._render_block("memory", ["old-secret-entry"])
    # the gateway observed the file at earlier requests (candidate history):
    enforce._memory_bodies_all(p)
    # mid-session save: the file grows
    p.memory_path.write_text("old-secret-entry\n\u00a7\nnew-entry-saved-later")
    payload = {"messages": [{"role": "system", "content": old_snapshot},
                            {"role": "user", "content": "hi"}], "model": "m"}
    result = enforce.on_llm_request(
        request=payload, session_id="hist", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m")
    assert result is not None  # history proved the old snapshot -> stripped
    content = result["request"]["messages"][0]["content"]
    assert "old-secret-entry" not in content


def test_forged_header_against_history_still_blocks(world):
    """Provenance is NOT weakened by history: a forged length-advertised header
    whose slice matches NO observed version stays intact and blocks."""
    p, _settings = world
    p.memory_path.parent.mkdir(parents=True, exist_ok=True)
    p.memory_path.write_text("real entry")
    from tools.memory_tool_store import MemoryStore
    rule = "\u2550" * 46
    forged = (f"{rule}\nMEMORY (your personal notes) [9% \u2014 24/2,200 chars]\n{rule}\n"
              "forged-fake-body")
    payload = {"messages": [{"role": "system", "content": forged},
                            {"role": "user", "content": "hi"}], "model": "m"}
    result = enforce.on_llm_request(
        request=payload, session_id="hist2", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m")
    assert result is None  # no observed version matches -> unprovable -> intact
    assert "forged-fake-body" in payload["messages"][0]["content"]
