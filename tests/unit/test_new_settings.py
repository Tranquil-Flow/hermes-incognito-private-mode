"""Unit tests: plaintext-remote block, persona strip, audit log (content-free)."""
import json
import time

import pytest


@pytest.fixture
def patch_capture(private_world, monkeypatch):
    """capture_paths -> private_world; pre-seed last_sweep so no daemon sweep races."""
    from ipm import enforce as enforce_mod
    from ipm import store
    monkeypatch.setattr(enforce_mod, "capture_paths", lambda: private_world)
    store.mutate("meta", private_world.data_dir,
                 lambda m: {**m, "last_sweep": {"at": time.time() + 3600}})
    return private_world


def _kwargs(request, base_url, provider="custom", model="m"):
    return {"request": request, "session_id": "s", "platform": "cli",
            "provider": provider, "base_url": base_url, "api_mode": "chat_completions",
            "model": model}


def test_plaintext_remote_blocked(patch_capture):
    from ipm.enforce import on_llm_execution
    kwargs = _kwargs({"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                     "http://insecure.example.net/v1")
    kwargs["next_call"] = lambda req: (_ for _ in ()).throw(AssertionError("must not reach provider"))
    response = on_llm_execution(**kwargs)
    assert response is not None
    assert response.choices[0].message.content.startswith("⛔")


def test_plaintext_loopback_allowed(patch_capture):
    from ipm.enforce import on_llm_execution
    seen = {}

    def next_call(request):
        seen["called"] = True
        return "ok"

    kwargs = _kwargs({"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                     "http://127.0.0.1:8391/v1")
    kwargs["next_call"] = next_call
    assert on_llm_execution(**kwargs) == "ok"
    assert seen.get("called") is True


def test_plaintext_remote_allowed_when_opted_in(patch_capture):
    from hermes_cli.plugins_state import save_plugin_setting
    from ipm.enforce import on_llm_execution
    save_plugin_setting("incognito-private-mode", ("allow_plaintext_remote",), True)
    save_plugin_setting("incognito-private-mode", ("allow_unverified",), True)  # self-contained
    try:
        seen = {}

        def next_call(request):
            seen["called"] = True
            return "ok"

        kwargs = _kwargs({"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                         "http://insecure.example.net/v1")
        kwargs["next_call"] = next_call
        assert on_llm_execution(**kwargs) == "ok"
        assert seen.get("called") is True
    finally:
        save_plugin_setting("incognito-private-mode", ("allow_plaintext_remote",), False)
        save_plugin_setting("incognito-private-mode", ("allow_unverified",), True)


def test_persona_stripped_from_system_prompt(patch_capture):
    from ipm.enforce import on_llm_request
    persona = ("You are Hermes Agent, built by Nous Research. " * 3).strip()
    (patch_capture.home / "SOUL.md").write_text(persona)
    from hermes_cli.plugins_state import save_plugin_setting
    save_plugin_setting("incognito-private-mode", ("strip_persona",), True)
    try:
        request = {"model": "m", "messages": [
            {"role": "system", "content": persona + "\n\nTool guidance here."},
            {"role": "user", "content": "hi"}]}
        result = on_llm_request(request=request, session_id="s", platform="cli",
                                provider="custom", base_url="http://127.0.0.1:8391/v1",
                                api_mode="chat_completions", model="m")
        assert result is not None
        content = result["request"]["messages"][0]["content"]
        assert persona not in content
        assert "[persona omitted: private mode]" in content
        assert "Tool guidance here." in content
    finally:
        save_plugin_setting("incognito-private-mode", ("strip_persona",), False)


def test_persona_absent_leaves_request_alone(patch_capture):
    from ipm.enforce import on_llm_request
    from hermes_cli.plugins_state import save_plugin_setting
    save_plugin_setting("incognito-private-mode", ("strip_persona",), True)
    try:
        # no SOUL.md on disk: no persona work, no mutation
        request = {"model": "m", "messages": [
            {"role": "system", "content": "plain"}, {"role": "user", "content": "hi"}]}
        result = on_llm_request(request=request, session_id="s", platform="cli",
                                provider="custom", base_url="http://127.0.0.1:8391/v1",
                                api_mode="chat_completions", model="m")
        assert result is None  # nothing changed
    finally:
        save_plugin_setting("incognito-private-mode", ("strip_persona",), False)


def test_audit_log_is_content_free(patch_capture):
    from ipm.enforce import on_llm_execution
    secret = "user-secret-content-abc"
    kwargs = _kwargs({"model": "m", "messages": [{"role": "user", "content": secret}]},
                     "https://openrouter.ai/api/v1", provider="openrouter")
    kwargs["next_call"] = lambda req: "provider-answer"
    on_llm_execution(**kwargs)
    audit = patch_capture.data_dir / "audit.jsonl"
    assert audit.exists()
    raw = audit.read_text()
    assert secret not in raw  # never content
    rec = json.loads(raw.splitlines()[-1])
    assert rec["provider"] == "openrouter"
    assert rec["label"] in {"ok-if-flagged", "ok", "warn"}
    assert isinstance(rec["blocked"], bool)
    assert set(rec.keys()) <= {"ts", "provider", "host", "scheme", "label",
                               "policy", "blocked", "persona"}
    assert rec["ts"] <= time.time() + 5


def test_new_defaults_are_off_or_on_as_designed():
    from ipm.settings import DEFAULTS
    assert DEFAULTS["allow_plaintext_remote"] is False  # cleartext egress blocked by default
    assert DEFAULTS["strip_persona"] is False  # opt-in; honest when it cannot strip
    assert DEFAULTS["audit_log"] is True  # local content-free trust log on by default
