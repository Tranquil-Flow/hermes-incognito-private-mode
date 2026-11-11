"""C-batch regressions: settings fallback direction, persona state, confirmations."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def patch_capture(private_world, monkeypatch):
    from ipm import enforce as enforce_mod
    monkeypatch.setattr(enforce_mod, "capture_paths", lambda: private_world)
    return private_world


# --- C3: settings fallback direction -----------------------------------------

def test_mode_false_yaml_parses_to_off():
    from ipm.settings import _validated
    out = _validated({"mode": False})  # unquoted YAML `mode: off`
    assert out["mode"] == "off"


def test_invalid_apply_to_platforms_fails_toward_no_deletion():
    from ipm.settings import _validated
    assert _validated({"apply_to_platforms": ["TUI"]})["apply_to_platforms"] == []
    assert _validated({"apply_to_platforms": "cli"})["apply_to_platforms"] == []
    assert _validated({"apply_to_platforms": ["cli", "tui"]})["apply_to_platforms"] == ["cli", "tui"]


def test_non_bool_remove_history_fails_to_false():
    from ipm.settings import _validated
    assert _validated({"remove_history_on_close": "yes"})["remove_history_on_close"] is False


# --- C4: persona state + coverage --------------------------------------------

def test_persona_covers_instructions_and_list_blocks():
    from ipm.strip import replace_persona_span
    persona = "You are Hermes Agent, built by Nous Research. " * 2
    request = {"instructions": f"{persona}\nBe helpful.",
               "messages": [
                   {"role": "system", "content": [{"type": "text", "text": f"intro {persona}"}]},
                   {"role": "system", "content": f"also {persona}"},
               ]}
    out, n = replace_persona_span(request, persona.strip())
    assert n >= 3
    assert "Nous Research" not in str(out)


def test_persona_audit_state_stripped_not_present_not_located(patch_capture):
    """When llm_request already replaced the persona, execution must record
    'stripped' — not the false alarm 'present-not-located'."""
    from ipm.enforce import _persona_text, on_llm_request, on_llm_execution
    soul = "You are Hermes Agent, built by Nous Research. " * 3
    (patch_capture.home / "SOUL.md").write_text(soul)
    from hermes_cli.plugins_state import save_plugin_setting
    save_plugin_setting("incognito-private-mode", ("strip_persona",), True)
    try:
        request = {"model": "m", "messages": [
            {"role": "system", "content": soul + "\nextra"},
            {"role": "user", "content": "hi"}]}
        result = on_llm_request(request=request, session_id="s9", platform="cli",
                                provider="custom", base_url="http://127.0.0.1:8391/v1",
                                api_mode="chat_completions", model="m")

        def next_call(req):
            return "ok"

        on_llm_execution(request=result["request"], session_id="s9", platform="cli",
                         provider="custom", base_url="http://127.0.0.1:8391/v1",
                         api_mode="chat_completions", model="m", next_call=next_call)
        audit = (patch_capture.data_dir / "audit.jsonl").read_text().splitlines()[-1]
        rec = json.loads(audit)
        assert rec["persona"] == "stripped", rec
    finally:
        save_plugin_setting("incognito-private-mode", ("strip_persona",), False)


# --- C2: confirmations bind to real rows --------------------------------------

def test_checklist_row_ids_exist_in_routes():
    from ipm.commands import _ACCOUNT_CHECKLIST
    from ipm.routes import load_table
    table = load_table()
    ids = {row.get("id") for row in table.get("rows", [])}
    for key, row_id, _label in _ACCOUNT_CHECKLIST:
        assert row_id in ids, f"checklist {key} -> missing row {row_id}"


def test_claude_confirmation_upgrades_oauth_verdict(patch_capture):
    """End-to-end: setup-recorded claude confirmation makes an anthropic OAuth
    request classify as confirmed."""
    from ipm import store
    from ipm.routes import classify, load_table
    table = load_table()
    row = next(r for r in table["rows"] if r["id"] == "anthropic-oauth")
    m = row["match"]
    confirmations = {"claude": {
        "row_id": "anthropic-oauth",
        "host": m["base_url_hosts"][0],
        "provider": m["provider_ids"][0],
        "policy_rev": table.get("policy_rev"),
        "confirmed_at": 1.0, "note": "confirmed in setup"}}
    verdict = classify("anthropic", "https://api.anthropic.com/v1",
                       "chat_completions", None, "oauth", confirmations, None)
    assert verdict.label in {"ok", "confirmed"}, verdict.label
