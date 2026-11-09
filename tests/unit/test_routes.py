"""Unit: route matching precedence, worst verdict, confirmations, coverage (§6 row 2)."""

from ipm.routes import (
    LABELS,
    classify,
    classify_background,
    classify_main,
    coverage_lines,
    host_of,
    is_loopback,
)


def test_worst_order():
    from ipm.routes import VERDICT_ORDER, _worst

    assert _worst(["ALREADY", "PER_REQUEST"]) == "PER_REQUEST"
    assert _worst(["ALREADY", "NONE_TRAINS", "UNVERIFIED"]) == "NONE_TRAINS"
    assert _worst([]) == "UNKNOWN"


def test_host_authority_beats_provider_id():
    # provider says openrouter, host is unknown -> never inherits openrouter's verdict
    v = classify("openrouter", "https://mirror.example.net/v1", "chat_completions", "m", "api_key", None)
    assert v.policy == "UNKNOWN"
    assert v.label == "warn"


def test_openrouter_route_ok_if_flagged():
    v = classify("openrouter", "https://openrouter.ai/api/v1", "chat_completions", "a/m", "api_key", None)
    assert v.policy == "PER_REQUEST"
    assert v.label == "ok-if-flagged"
    assert v.flags.get("data_collection") == "deny"
    assert v.flags.get("zdr") is True


def test_bigmodel_blocks():
    v = classify("zai", "https://open.bigmodel.cn/api/paas/v4", "chat_completions", "glm-4", "api_key", None)
    assert v.policy == "NONE_TRAINS"
    assert v.label == "block"


def test_loopback_local_is_already():
    v = classify("custom", "http://127.0.0.1:11434/v1", "chat_completions", "llama", "api_key", None)
    assert v.policy == "ALREADY"
    assert v.label == "ok"


def test_anthropic_auth_split():
    key = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "claude-x", "api_key", None)
    assert key.policy == "ALREADY"
    # unknown auth -> candidates considered, worst wins (conservative)
    unknown = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "claude-x", None, None)
    assert unknown.policy == "ACCOUNT_SETTING"


def test_confirmation_upgrades_to_confirmed():
    table_label = classify("nous", "https://api.nousresearch.com", "chat_completions", "n", None, None)
    assert table_label.policy in {"PER_REQUEST", "ACCOUNT_SETTING"}
    confirmations = {
        "nous-portal": {"row_id": "nous-portal", "host": "api.nousresearch.com",
                        "provider": "nous", "policy_rev": "2026-11-10-r1"}
    }
    v = classify("nous", "https://api.nousresearch.com", "chat_completions", "n", None, confirmations)
    assert v.label == "ok-if-flagged"  # PER_REQUEST confirmed still needs the flag
    # ACCOUNT_SETTING rows become confirmed->ok
    v2 = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth",
                  {"anthropic-oauth": {"row_id": "anthropic-oauth", "host": "api.anthropic.com",
                                       "provider": "anthropic", "policy_rev": "2026-11-10-r1"}})
    assert v2.label == "confirmed"


def test_stale_policy_rev_confirmation_ignored():
    confirmations = {"nous-portal": {"row_id": "nous-portal", "host": "api.nousresearch.com",
                                     "provider": "nous", "policy_rev": "OLD"}}
    v = classify("nous", "https://api.nousresearch.com", "chat_completions", "n", None, confirmations)
    assert v.confirmed is False


def test_main_coverage_worst_of_fallbacks():
    facts = {
        "main": {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "m"},
        "fallbacks": [{"provider": "zai", "base_url": "https://open.bigmodel.cn", "model": "g"}],
        "aux": [],
    }
    v = classify_main(facts)
    assert v.policy == "NONE_TRAINS"  # worst of main + fallbacks (§3.7)
    assert "zhipu-cn" in v.row_ids


def test_background_unknown_when_auto():
    v = classify_background({"main": {}, "fallbacks": [], "aux": []})
    assert v.policy == "UNKNOWN"
    assert "auto routing" in " ".join(v.reasons) or "auto-route" in " ".join(v.reasons)


def test_background_explicit_aux_route():
    facts = {"main": {}, "fallbacks": [],
             "aux": [{"task": "title_generation", "provider": "openrouter",
                      "base_url": "https://openrouter.ai/api/v1", "model": "x"}]}
    v = classify_background(facts)
    assert v.policy == "PER_REQUEST"


def test_coverage_lines_three_parts():
    facts = {"main": {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m"},
             "fallbacks": [], "aux": []}
    lines = coverage_lines(facts, None, None, {}, cleanup_active=True)
    assert len(lines) >= 3
    assert lines[0].startswith("1. Main chat requests")
    assert lines[1].startswith("2. Background calls")
    assert lines[2].startswith("3. On this machine")
    assert "some copies remain" in lines[2]


def test_p1_routes_not_blessed():
    v = classify("commandcode", None, "chat_completions", "x", "api_key", None)
    assert v.label == "warn"  # transform not implemented in P0 (§3.7)
    assert any("P1" in r or "implemented" in r for r in v.reasons)
