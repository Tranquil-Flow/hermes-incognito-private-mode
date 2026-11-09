"""Unit: enforce middleware decisions + lifecycle state machine (§6 row 2)."""

import sqlite3
import time
from conftest import FakePaths

import pytest

from ipm import lifecycle, store
from ipm.enforce import on_llm_execution, on_llm_request, on_pre_tool_call
from ipm.settings import DEFAULTS


# --- schema helper for lifecycle tests ---------------------------------------

def make_db(paths, sessions):
    """Build a REAL SessionDB schema (minimal tables break core's SCHEMA_SQL),
    then insert the test rows. A row may carry (id, source, parent, started,
    ended, reason) or a 7th model_config JSON (delegate marker)."""
    from hermes_state import SessionDB

    db = SessionDB(paths.db_path)
    db.close()
    conn = sqlite3.connect(str(paths.db_path))
    for row in sessions:
        model_config = row[6] if len(row) > 6 else None
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason, model_config) "
            "VALUES (?,?,?,?,?,?,?)", (*row[:6], model_config))
    conn.commit()
    conn.close()


# --- enforce -------------------------------------------------------------------

@pytest.fixture
def patch_capture(monkeypatch, private_world):
    """Route enforce.capture_paths() to the fixture world (hooks normally capture
    the ambient home; in tests the ambient home IS the harness, so we pin it)."""
    from ipm import enforce as enforce_mod
    monkeypatch.setattr(enforce_mod, "capture_paths", lambda: private_world)
    # Pre-seed last_sweep so observe_* never spawns a racing daemon sweep.
    store.mutate("meta", private_world.data_dir,
                 lambda m: {**m, "last_sweep": {"at": time.time() + 3600}})
    return private_world


def test_llm_request_applies_flags_and_strip(patch_capture):
    from ipm import store

    store.mutate("ledger", patch_capture.data_dir, lambda l: l)
    request = {"model": "m", "messages": [
        {"role": "user", "content": "hi <memory-context>recall</memory-context>"}]}
    kwargs = {"request": request, "session_id": "sess-1", "platform": "cli",
              "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
              "api_mode": "chat_completions", "model": "m"}
    result = on_llm_request(**kwargs)
    assert result is not None
    new_request = result["request"]
    assert new_request is not request
    assert new_request["extra_body"]["provider"]["zdr"] is True
    assert "<memory-context>" not in new_request["messages"][0]["content"]
    # lazy observation tombstoned the unknown session (policy only, §3.3)
    ledger = store.read("ledger", patch_capture.data_dir)
    from ipm.store import hash_session_id
    assert ledger[hash_session_id("sess-1")]["state"] == "excluded"


def test_llm_request_off_mode_passthrough(patch_capture):
    from hermes_cli.plugins_state import save_plugin_setting

    save_plugin_setting("incognito-private-mode", ("mode",), "off")
    try:
        request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        result = on_llm_request(request=request, session_id="s", platform="cli",
                                provider="openrouter", base_url="https://openrouter.ai/api/v1",
                                api_mode="chat_completions", model="m")
        assert result is None
    finally:
        save_plugin_setting("incognito-private-mode", ("mode",), "private")


def test_llm_execution_blocks_training_route(patch_capture):
    kwargs = {"request": {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
              "session_id": "s", "platform": "cli", "provider": "zai",
              "base_url": "https://open.bigmodel.cn/api", "api_mode": "chat_completions",
              "model": "glm", "next_call": lambda req: (_ for _ in ()).throw(AssertionError("must not call"))}
    response = on_llm_execution(**kwargs)
    assert response is not None
    assert response.choices[0].message.content.startswith("⛔")


def test_llm_execution_second_chance_flags(patch_capture):
    seen = {}

    def next_call(request):
        seen["request"] = request
        return "provider-answer"

    kwargs = {"request": {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
              "session_id": "s", "platform": "cli", "provider": "openrouter",
              "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
              "model": "m", "next_call": next_call}
    response = on_llm_execution(**kwargs)
    assert response == "provider-answer"
    assert seen["request"]["extra_body"]["provider"]["data_collection"] == "deny"


def test_llm_execution_memory_block_block(patch_capture):
    rule = "═" * 46
    block = f"{rule}\nMEMORY (your personal notes) [1%]\n{rule}\nsecret note"
    kwargs = {"request": {"model": "m", "messages": [{"role": "system", "content": block},
                                                     {"role": "user", "content": "hi"}]},
              "session_id": "s", "platform": "cli", "provider": "custom",
              "base_url": "http://127.0.0.1:9/v1", "api_mode": "chat_completions",
              "model": "m", "next_call": lambda req: "answer"}
    response = on_llm_execution(**kwargs)
    assert response is not None  # blocked: memory content still present


def test_pre_tool_call_vetoes_and_fail_closed(patch_capture):
    result = on_pre_tool_call(tool_name="memory", session_id="s")
    assert result == {"action": "block", "message":
                      "Blocked by private mode: memory saving is off for this chat."}
    assert on_pre_tool_call(tool_name="terminal", session_id="s") is None


# --- lifecycle -----------------------------------------------------------------

def test_candidate_requires_enrollment(private_world):
    # no consent yet
    paths = FakePaths(private_world.home.parent / "noconsent")
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    lifecycle.observe_session_start("sess-a", "cli", paths)
    ledger = store.read("ledger", paths.data_dir)
    from ipm.store import hash_session_id
    assert ledger[hash_session_id("sess-a")]["state"] == "excluded"
    # with consent + generation: candidate
    lifecycle.observe_session_start("sess-b", "cli", private_world)
    ledger = store.read("ledger", private_world.data_dir)
    assert ledger[hash_session_id("sess-b")]["state"] == "candidate"


def test_platform_out_of_scope_excluded(private_world):
    lifecycle.observe_session_start("sess-msg", "telegram", private_world)
    ledger = store.read("ledger", private_world.data_dir)
    from ipm.store import hash_session_id
    assert ledger[hash_session_id("sess-msg")]["state"] == "excluded"


def test_arming_needs_generation_span(private_world):
    from ipm.store import hash_session_id

    now = time.time()
    gen = store.current_generation(store.read("meta", private_world.data_dir))
    make_db(private_world, [
        ("new-sess", "cli", None, now + 1, None, None),
        ("old-sess", "cli", None, now - 999999, None, None),
    ])
    lifecycle.observe_session_start("new-sess", "cli", private_world)
    lifecycle.observe_session_start("old-sess", "cli", private_world)
    counts = lifecycle.run_sweep(private_world, batch_limit=20)
    ledger = store.read("ledger", private_world.data_dir)
    assert ledger[hash_session_id("new-sess")]["state"] == "armed"
    assert ledger[hash_session_id("old-sess")]["state"] == "excluded"  # row 13 semantics
    assert counts.get("armed", 0) >= 1


def test_delete_only_closed_allowlist_reasons(private_world):
    from ipm.store import hash_session_id

    now = time.time()
    make_db(private_world, [
        ("closed-sess", "cli", None, now + 1, now + 100, "cli_close"),
        ("auto-sess", "desktop", None, now + 1, now + 100, "ws_disconnect"),
        ("open-sess", "cli", None, now + 1, None, None),
    ])
    for sid in ("closed-sess", "auto-sess", "open-sess"):
        lifecycle.observe_session_start(sid, "cli", private_world)
    lifecycle.run_sweep(private_world, batch_limit=20)
    ledger = store.read("ledger", private_world.data_dir)
    # closed-sess deleted; automatic + open rows survive (rows 11/§3.3)
    assert ledger[hash_session_id("closed-sess")]["state"] == "deleted"
    assert ledger[hash_session_id("auto-sess")]["state"] in {"armed"}
    assert ledger[hash_session_id("open-sess")]["state"] in {"armed"}
    row = lifecycle.read_row(private_world.db_path, "closed-sess")
    assert row is None
    assert lifecycle.read_row(private_world.db_path, "auto-sess") is not None


def test_unadmitted_neighbour_never_touched(private_world):
    """Row 17: neighbour ids are only affected when they are armed members of the
    same ledgered lineage; an unadmitted neighbour always survives."""
    from ipm.store import hash_session_id

    now = time.time()
    make_db(private_world, [
        ("abc", "cli", None, now + 1, now + 100, "cli_close"),
        ("abc-extra", "cli", None, now + 1, now + 101, "cli_close"),  # never observed
        ("abc_extra", "cli", None, now + 1, now + 102, "cli_close"),  # never observed
    ])
    lifecycle.observe_session_start("abc", "cli", private_world)
    lifecycle.run_sweep(private_world, batch_limit=20)
    assert lifecycle.read_row(private_world.db_path, "abc") is None
    assert lifecycle.read_row(private_world.db_path, "abc-extra") is not None
    assert lifecycle.read_row(private_world.db_path, "abc_extra") is not None


def test_fenced_delete_refuses_unadmitted_lineage(private_world):
    now = time.time()
    delegate_marker = '{"_delegate_from": "tip"}'
    make_db(private_world, [
        ("tip", "cli", None, now + 1, now + 100, "cli_close"),
        ("child", "delegate", "tip", now + 2, now + 103, "cli_close", delegate_marker),
    ])
    lifecycle.observe_session_start("tip", "cli", private_world)
    counts = lifecycle.run_sweep(private_world, batch_limit=20)
    # fence refused the whole operation: tip still present (B1 semantics)
    assert lifecycle.read_row(private_world.db_path, "tip") is not None
    assert lifecycle.read_row(private_world.db_path, "child") is not None


def test_keep_survives_lineage(private_world):
    from ipm.store import hash_session_id

    now = time.time()
    make_db(private_world, [("kept", "cli", None, now + 1, now + 50, "cli_close")])
    lifecycle.observe_session_start("kept", "cli", private_world)
    lifecycle.set_override("kept", "keep", private_world)
    lifecycle.run_sweep(private_world, batch_limit=20)
    assert lifecycle.read_row(private_world.db_path, "kept") is not None
    ledger = store.read("ledger", private_world.data_dir)
    assert ledger[hash_session_id("kept")]["state"] == "excluded"


def test_no_consent_no_deletion(tmp_path):
    paths = FakePaths(tmp_path)
    paths.home.mkdir(exist_ok=True)
    paths.data_dir.mkdir(exist_ok=True)
    now = time.time()
    make_db(paths, [("x", "cli", None, now + 1, now + 5, "cli_close")])
    lifecycle.observe_session_start("x", "cli", paths)  # excluded: not enrolled
    # force candidate for the deletion path check
    def fn(ledger):
        from ipm.store import hash_session_id
        ledger[hash_session_id("x")] = {"state": "candidate", "raw_id": "x",
                                        "generation": 1, "platform": "cli",
                                        "parent_hash": None, "children": [], "attempts": 0}
        return ledger
    store.mutate("ledger", paths.data_dir, fn)
    counts = lifecycle.run_sweep(paths, batch_limit=20)
    assert lifecycle.read_row(paths.db_path, "x") is not None  # row 16
    assert counts.get("armed", 0) == 0
