"""Adversarial-round regressions: memory-strip wiring, preset CLI, notice honesty.

Pins the three HIGH findings from the 2026-11-11 fresh-eyes round:
1. CapturedPaths must expose memory/user-profile paths, and a strip whose
   exact reconstruction fails must BLOCK (secrets never reach next_call).
2. `preset <name>` must work through the REAL parser (dest correctness).
3. The finalize notice must disclose lineage members that remain.
"""
import argparse
import time

import pytest


@pytest.fixture
def patch_capture(private_world, monkeypatch):
    from ipm import commands as commands_mod
    from ipm import enforce as enforce_mod
    monkeypatch.setattr(commands_mod, "capture_paths", lambda: private_world)
    monkeypatch.setattr(enforce_mod, "capture_paths", lambda: private_world)
    from ipm import store
    store.mutate("meta", private_world.data_dir,
                 lambda m: {**m, "last_sweep": {"at": time.time() + 3600}})
    return private_world


# --- HIGH-1: memory-strip wiring + ambiguity block ---------------------------

def test_captured_paths_expose_memory_files(private_world):
    from ipm.settings import capture_paths
    paths = capture_paths()
    assert str(paths.memory_path).endswith("MEMORY.md")
    assert str(paths.user_profile_path).endswith("USER.md")
    assert paths.memory_path.parent.name == "memories"


def _write_real_memory(private_world, body: str) -> str:
    mem = private_world.home / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text(body)
    return body


def test_blank_line_entry_blocks_and_never_reaches_provider(patch_capture):
    """A well-formed core block (exact usage figure) strips cleanly even with
    blank lines inside entries. A FORGED block missing the usage figure cannot
    be proven fully removed -> BLOCKED, never sent (fail closed)."""
    from ipm.enforce import on_llm_request, on_llm_execution
    rule = "\u2550" * 46

    def build(body: str, title: str) -> dict:
        block = f"{rule}\n{title}\n{rule}\n{body}"
        return {"model": "m", "messages": [
            {"role": "system", "content": block},
            {"role": "user", "content": "hi"}]}

    def next_call(req):
        return "provider-answer"

    # (a) blank lines inside an entry, header intact -> strips cleanly
    body_a = "entry one para\n\nSECRET-PARA-2\n\u00a7SECRET-ENTRY-2"
    title_a = f"MEMORY (your personal notes) [41% \u2014 {len(body_a):,}/2,200 chars]"
    _write_real_memory(patch_capture, body_a + "\n")
    res = on_llm_request(request=build(body_a, title_a), session_id="s1",
                         platform="cli", provider="custom",
                         base_url="http://127.0.0.1:8391/v1",
                         api_mode="chat_completions", model="m")
    assert res is not None
    assert "SECRET-PARA-2" not in str(res["request"])
    assert on_llm_execution(request=res["request"], session_id="s1", platform="cli",
                            provider="custom", base_url="http://127.0.0.1:8391/v1",
                            api_mode="chat_completions", model="m",
                            next_call=next_call) == "provider-answer"

    # (b) forged block: no usage figure -> ambiguity -> BLOCK, never sent
    forged_title = "MEMORY (your personal notes) [41%]"
    body_b = "entry one para\n\nSECRET-FORGED-PARA"
    res2 = on_llm_request(request=build(body_b, forged_title), session_id="s1b",
                          platform="cli", provider="custom",
                          base_url="http://127.0.0.1:8391/v1",
                          api_mode="chat_completions", model="m")
    assert res2 is None  # unprovable strip: request phase leaves it intact

    def next_call2(req):
        raise AssertionError("request with unproven memory strip reached the provider")

    out = on_llm_execution(request=build(body_b, forged_title), session_id="s1b",
                           platform="cli", provider="custom",
                           base_url="http://127.0.0.1:8391/v1",
                           api_mode="chat_completions", model="m", next_call=next_call2)
    assert out is not None and "provider-answer" != out


def test_exact_reconstruction_strips_cleanly(patch_capture):
    """Core-real formats: the FILE is entries-only; the PROMPT header carries
    the exact snapshot length. Primary path (header-length slice) strips
    exactly even when the file has DIVERGED since the snapshot was taken
    (mid-session save, round-#3 review); request passes with no memory text
    on the wire."""
    from ipm.enforce import on_llm_request, on_llm_execution
    rule = "\u2550" * 46
    body = "SECRET-ENTRY-ONE"
    title = f"MEMORY (your personal notes) [99% \u2014 {len(body):,}/2,200 chars]"
    # file DIVERGED after the snapshot (extra entry saved mid-session); the
    # gateway observed the pre-save version at earlier requests (history)
    from ipm.enforce import _memory_bodies_all
    _write_real_memory(patch_capture, body)
    _memory_bodies_all(patch_capture)
    _write_real_memory(patch_capture, f"{body}\n\u00a7\nENTRY-SAVED-LATER\n")
    block = f"{rule}\n{title}\n{rule}\n{body}"
    request = {"model": "m", "messages": [
        {"role": "system", "content": block},
        {"role": "user", "content": "hi"}]}
    result = on_llm_request(
        request=request, session_id="s2", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions", model="m")
    # bounded history: the pre-save file version the session observed proves
    # the frozen snapshot, so the block strips EXACTLY (availability fix,
    # round-#4 Opus F2) and nothing memory-bearing reaches the provider.
    assert result is not None
    final = result["request"]
    assert "SECRET-ENTRY-ONE" not in str(final)
    # the snapshot block is fully removed (header + exact body); the newer
    # entry lives in the FILE, never in the frozen snapshot, so nothing of it
    # was on the wire to begin with
    assert "ENTRY-SAVED-LATER" not in str(final)

    def next_call(req):
        return "provider-answer"

    out = on_llm_execution(
        request=final, session_id="s2", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions",
        model="m", next_call=next_call)
    assert out == "provider-answer"  # clean: no residue, nothing blocked


# --- HIGH-3: preset CLI through the REAL parser ------------------------------

def test_preset_cli_through_real_parser(patch_capture, capsys):
    from ipm import commands as commands_mod
    from ipm.presets import PRESETS
    from ipm.settings import DEFAULTS
    from hermes_cli.plugins_state import save_plugin_setting

    saved = {k: DEFAULTS[k] for k in PRESETS["complete"]}
    parser = argparse.ArgumentParser()
    commands_mod.cli_register(parser)
    args = parser.parse_args(["preset", "complete"])
    try:
        rc = commands_mod.cli_handler(args, paths=patch_capture)
        assert rc == 0
        out = capsys.readouterr().out
        assert "complete" in out and "unknown preset" not in out
    finally:
        for key, value in saved.items():
            save_plugin_setting("incognito-private-mode", (key,), value)


# --- HIGH-2: notice discloses surviving lineage members ----------------------

def test_finalize_discloses_remaining_lineage(patch_capture):
    from ipm import store
    from ipm.lifecycle import finalize
    from ipm.store import hash_session_id as _h

    parent_hash = _h("par-1")
    child_hash = _h("tip-1")
    store.mutate("ledger", patch_capture.data_dir, lambda l: {
        **l,
        parent_hash: {"state": "armed", "raw_id": "par-1",
                      "children": [child_hash], "created_ts": time.time()},
        child_hash: {"state": "deleted", "deleted_how": "fenced",
                     "parent_hash": parent_hash, "raw_id": "tip-1",
                     "created_ts": time.time()},
    })
    notice = finalize("tip-1", "cli", patch_capture)
    assert notice is not None
    assert "earlier part(s) of this chat remain" in notice


def test_finalize_no_false_disclosure_without_lineage(patch_capture):
    from ipm import store
    from ipm.lifecycle import finalize
    from ipm.store import hash_session_id as _h

    key = _h("solo-1")
    store.mutate("ledger", patch_capture.data_dir, lambda l: {
        **l, key: {"state": "deleted", "deleted_how": "fenced", "raw_id": "solo-1",
                   "created_ts": time.time()}})
    notice = finalize("solo-1", "cli", patch_capture)
    assert notice is not None
    assert "earlier part(s)" not in notice


# --- host-authoritative classification (adversarial round MEDIUM) -----------

def test_custom_provider_on_curated_training_host_gets_host_policy():
    """'custom' + api.moonshot.ai must see kimi's NONE_TRAINS policy (block),
    never UNKNOWN/warn — the host is authoritative."""
    from ipm.routes import classify
    verdict = classify("custom", "https://api.moonshot.ai/v1", "chat_completions",
                       None, None, None, None)
    assert verdict.label == "block", f"got {verdict.label}: {verdict.reasons}"


def test_custom_provider_on_curated_openrouter_host_gets_flags():
    from ipm.routes import classify
    verdict = classify("custom", "https://openrouter.ai/api/v1", "chat_completions",
                       None, None, None, None)
    assert verdict.label in {"ok-if-flagged", "ok"}, verdict.label
