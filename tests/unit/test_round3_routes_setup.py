"""Round-3 regressions: route confirmation scope, uncurated-host caps, status /
setup honesty (main_label vs enforcement, surfaces, consent text, wizard).

Every setup test drives the REAL ``run_setup`` (mocked ``input``) and the REAL
``write_confirmations`` store write — never a hand-built confirmations dict
alone — then reads confirmations.json back and classifies with it.
"""

from __future__ import annotations

import copy
import json

import pytest

from ipm import routes as routes_mod
from ipm.routes import classify

REV = routes_mod.load_table().get("policy_rev")


# --- helpers -------------------------------------------------------------------

def _synthetic_table():
    """Rows that do not exist in routes.json, to exercise shapes the shipped
    table lacks (multi-row worst, provider-only flags/confirmable rows)."""
    return {
        "policy_rev": "syn-1",
        "rows": [
            {"id": "acct-a", "match": {"base_url_hosts": ["multi.example"], "provider_ids": ["multi"],
                                       "auth": "any"},
             "verdict": "ACCOUNT_SETTING", "account_setting": "toggle A",
             "sources": [{"url": "https://src.example/a"}], "policy_rev": "syn-1"},
            {"id": "org-b", "match": {"base_url_hosts": ["multi.example"], "provider_ids": ["multi"],
                                      "auth": "any"},
             "verdict": "ORG_AGREEMENT", "account_setting": "agreement B",
             "sources": [{"url": "https://src.example/b"}], "policy_rev": "syn-1"},
            {"id": "ok-c", "match": {"base_url_hosts": ["multi.example"], "provider_ids": ["multi"],
                                     "auth": "any"},
             "verdict": "ALREADY", "sources": [{"url": "https://src.example/c"}], "policy_rev": "syn-1"},
            {"id": "acct-ret", "match": {"base_url_hosts": ["ret.example"], "provider_ids": ["ret"],
                                         "auth": "any"},
             "verdict": "ACCOUNT_SETTING", "sources": [], "policy_rev": "syn-1"},
            {"id": "ret-n", "match": {"base_url_hosts": ["ret.example"], "provider_ids": ["ret"],
                                      "auth": "any"},
             "verdict": "NONE_RETENTION", "sources": [], "policy_rev": "syn-1"},
            {"id": "alias-row", "match": {"base_url_hosts": ["a1.example", "a2.example"],
                                          "provider_ids": ["alias"], "auth": "oauth"},
             "verdict": "ACCOUNT_SETTING", "sources": [], "policy_rev": "syn-1"},
            {"id": "po-flags", "match": {"base_url_hosts": [], "provider_ids": ["poflag"], "auth": "any"},
             "verdict": "PER_REQUEST", "flags": {"kind": "provider_routing", "zdr": True},
             "sources": [], "policy_rev": "syn-1"},
            {"id": "po-acct", "match": {"base_url_hosts": [], "provider_ids": ["poacct"], "auth": "any"},
             "verdict": "ACCOUNT_SETTING", "account_setting": "org ZDR",
             "sources": [], "policy_rev": "syn-1"},
            {"id": "po-train", "match": {"base_url_hosts": [], "provider_ids": ["potrain"], "auth": "any"},
             "verdict": "NONE_TRAINS", "sources": [], "policy_rev": "syn-1"},
        ],
    }


@pytest.fixture
def syn_table(monkeypatch):
    table = _synthetic_table()
    monkeypatch.setattr(routes_mod, "load_table", lambda data_dir=None: table)
    return table


def _conf(row_id, host, rev, **extra):
    rec = {"row_id": row_id, "host": host, "policy_rev": rev}
    rec.update(extra)
    return rec


# --- (1) uncurated hosts: positive/confirmed capped, NONE_* retained ---------------

def test_already_provider_only_unknown_host_capped():
    v = classify("opencode-zen", "https://attacker.example/v1", "chat_completions", "m", "api_key", None)
    assert v.label == "warn" and v.cap_reason == "uncurated-host"
    # control: empty host (runtime canonical provider) keeps existing behaviour
    c = classify("opencode-zen", None, "chat_completions", "m", "api_key", None)
    assert c.label == "ok"


def test_per_request_provider_only_unknown_host_capped_flags_retained(syn_table):
    v = classify("poflag", "https://evil.example/v1", "chat_completions", "m", None, None,
                 transform_implemented=True)
    assert v.label == "warn", v
    assert v.flags.get("kind") == "provider_routing"  # flags still applied by enforce
    control = classify("poflag", None, "chat_completions", "m", None, None, transform_implemented=True)
    assert control.label == "ok-if-flagged"


def test_account_setting_provider_only_unknown_host_never_confirmed(syn_table):
    # even a confirmation bound EXACTLY to the unknown host cannot bless it
    confs = {"x": _conf("po-acct", "evil.example", "syn-1")}
    v = classify("poacct", "https://evil.example/v1", None, None, None, confs)
    assert v.label == "warn" and v.confirmed is False
    assert v.cap_reason == "uncurated-host"
    # unconfirmed: also warn (no 'needs-confirmation' — confirming cannot help)
    u = classify("poacct", "https://evil.example/v1", None, None, None, None)
    assert u.label == "warn"


def test_empty_host_provider_only_confirmation_applies_only_to_empty_host(syn_table):
    wildcard = {"x": _conf("po-acct", "", "syn-1")}
    # the old broad promotion: a host-less confirmation applied to ANY real host
    assert classify("poacct", "https://any.example", None, None, None, wildcard).label == "warn"
    assert classify("poacct", "https://any.example", None, None, None, wildcard).confirmed is False
    # exact empty host (runtime canonical provider): existing behaviour kept
    assert classify("poacct", None, None, None, None, wildcard).label == "confirmed"


def test_nebius_wildcard_confirmation_removed():
    confs = {"nebius": _conf("nebius", "", REV, provider="nebius")}
    v = classify("nebius", "https://attacker.example/v1", "chat_completions", "m", "api_key", confs)
    assert v.label == "warn" and v.confirmed is False


def test_none_trains_provider_only_unknown_host_still_blocks(syn_table):
    v = classify("potrain", "https://evil.example/v1", None, None, None, None)
    assert v.label == "block" and v.policy == "NONE_TRAINS"


def test_real_qwen_oauth_unknown_host_still_blocks_and_xiaomi_warns():
    q = classify("qwen-oauth", "https://portal.unknown.example", None, None, "oauth", None)
    assert q.label == "block"
    x = classify("xiaomi", "https://mimo.unknown.example", None, None, "api_key", None)
    assert x.label == "warn" and x.policy == "NONE_RETENTION"


@pytest.mark.parametrize("provider", ["custom", "openrouter", "anthropic", None, "kimi"])
@pytest.mark.parametrize("url", ["https://api.moonshot.ai/v1", "https://open.bigmodel.cn/api/paas/v4",
                                 "https://api.mistral.ai/v1",
                                 "https://generativelanguage.googleapis.com/v1beta"])
def test_curated_training_host_blocks_regardless_of_provider_label(provider, url):
    v = classify(provider, url, "chat_completions", "m", None, None)
    assert v.label == "block", (provider, url, v)


@pytest.mark.parametrize("auth", ["oauth", "api_key"])
def test_curated_training_host_blocks_on_auth_mismatch(auth):
    # anthropic_messages + Claude-Code marker gives auth 'oauth'; kimi rows are
    # api_key — a mismatched auth must not turn a training host into UNKNOWN
    v = classify("custom", "https://api.moonshot.ai/anthropic", "anthropic_messages", "k2", auth, None)
    assert v.label == "block", v


def test_host_mismatch_fallback_never_positive():
    # api.openai.com rows do not list this api mode; worst host row, capped
    v = classify("openai", "https://api.openai.com/v1", "bedrock_converse", "m", "api_key", None)
    assert v.label == "warn" and v.cap_reason == "host-mismatch"


# --- (2) confirmations: worst rows only, scoped ------------------------------------

def test_multi_row_worst_requires_every_confirmable_row(syn_table):
    a = _conf("acct-a", "multi.example", "syn-1")
    b = _conf("org-b", "multi.example", "syn-1")
    c = _conf("ok-c", "multi.example", "syn-1")
    url = "https://multi.example/v1"
    assert classify("multi", url, None, None, None, None).label == "needs-confirmation"
    assert classify("multi", url, None, None, None, {"a": a}).label == "needs-confirmation"
    assert classify("multi", url, None, None, None, {"b": b}).label == "needs-confirmation"
    assert classify("multi", url, None, None, None, {"c": c}).label == "needs-confirmation"
    both = classify("multi", url, None, None, None, {"a": a, "b": b})
    assert both.label == "confirmed" and both.confirmed is True


def test_confirming_better_row_never_upgrades_worse_policy(syn_table):
    v = classify("ret", "https://ret.example", None, None, None,
                 {"x": _conf("acct-ret", "ret.example", "syn-1")})
    assert v.policy == "NONE_RETENTION" and v.label == "warn" and v.confirmed is False


def test_gemini_paid_confirmation_cannot_unblock_gemini_api():
    confs = {"g": _conf("gemini-paid", "generativelanguage.googleapis.com", REV)}
    v = classify("gemini", "https://generativelanguage.googleapis.com/v1beta", None, None, "api_key", confs)
    assert v.label == "block"


def test_confirmation_requires_record_row_id_not_dict_key():
    confs = {"anthropic-oauth": {"host": "api.anthropic.com", "policy_rev": REV}}
    v = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth", confs)
    assert v.label == "needs-confirmation"


def test_host_curated_row_rejects_empty_host_wildcard_record():
    confs = {"claude": _conf("anthropic-oauth", "", REV)}
    v = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth", confs)
    assert v.label == "needs-confirmation"


def test_profile_scope(tmp_path):
    p1, p2 = str(tmp_path / "p1"), str(tmp_path / "p2")
    url = "https://api.anthropic.com"
    rec = _conf("anthropic-oauth", "api.anthropic.com", REV, auth="oauth",
                profile=routes_mod.profile_scope(p1))
    assert classify("anthropic", url, "anthropic_messages", "c", "oauth", {"k": rec},
                    profile=p1).label == "confirmed"
    assert classify("anthropic", url, "anthropic_messages", "c", "oauth", {"k": rec},
                    profile=p2).label == "needs-confirmation"
    legacy = _conf("anthropic-oauth", "api.anthropic.com", REV)  # no profile recorded
    assert classify("anthropic", url, "anthropic_messages", "c", "oauth", {"k": legacy},
                    profile=p1).label == "needs-confirmation"
    # unwired call sites (profile=None) keep positional compatibility
    assert classify("anthropic", url, "anthropic_messages", "c", "oauth", {"k": rec}).label == "confirmed"


def test_auth_scope():
    rec = _conf("anthropic-oauth", "api.anthropic.com", REV, auth="api_key")
    v = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth", {"k": rec})
    assert v.label == "needs-confirmation"


def test_policy_rev_scope():
    rec = _conf("anthropic-oauth", "api.anthropic.com", "OLD", auth="oauth")
    v = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth", {"k": rec})
    assert v.label == "needs-confirmation"


@pytest.mark.parametrize("provider", ["anthropic", "claude", "custom"])
def test_provider_aliases_share_confirmed_row(provider):
    rec = _conf("anthropic-oauth", "api.anthropic.com", REV, auth="oauth", provider="anthropic")
    v = classify(provider, "https://api.anthropic.com/v1", "anthropic_messages", "c", "oauth", {"k": rec})
    assert v.label == "confirmed", provider


def test_row_host_aliases_share_confirmed_row(syn_table):
    legacy_first_host_only = _conf("alias-row", "a1.example", "syn-1")
    for host in ("a1.example", "a2.example"):
        v = classify("alias", f"https://{host}", None, None, "oauth", {"k": legacy_first_host_only})
        assert v.label == "confirmed", host
    # a host outside the row is never covered (no wildcard)
    v = classify("alias", "https://a3.example", None, None, "oauth", {"k": legacy_first_host_only})
    assert v.label == "warn"


def test_verdict_exposes_sources():
    v = classify("zai", "https://open.bigmodel.cn/api", None, None, None, None)
    assert v.sources and all(isinstance(s, str) and s for s in v.sources)


def test_classify_main_worst_by_label_not_policy():
    # main ALREADY-but-capped (warn) + fallback ok-if-flagged: the line must not be ✅
    facts = {"main": {"provider": "opencode-zen", "base_url": "https://attacker.example/v1", "model": "m"},
             "fallbacks": [{"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "x"}],
             "aux": []}
    v = routes_mod.classify_main(facts)
    assert v.label == "warn"


def test_status_prediction_caps_runtime_resolved_provider_only_route():
    facts = {"main": {"provider": "opencode-zen", "base_url": None, "model": "m"}, "fallbacks": [], "aux": []}
    v = routes_mod.classify_main(facts)
    assert v.label == "warn" and v.cap_reason == "runtime-endpoint"


# --- (3) main_label vs enforcement (table-driven, real enforcement decision) ----------

_VERDICT_CASES = {
    "ok": dict(provider="xai", base_url="https://api.x.ai/v1", api_mode="chat_completions"),
    "ok-if-flagged": dict(provider="openrouter", base_url="https://openrouter.ai/api/v1",
                          api_mode="chat_completions"),
    "block": dict(provider="zai", base_url="https://open.bigmodel.cn/api/paas/v4",
                  api_mode="chat_completions"),
    "warn-unverified": dict(provider="deepseek", base_url="https://api.deepseek.com/v1",
                            api_mode="chat_completions"),
    "warn-unknown": dict(provider="custom", base_url="https://unknown.example/v1",
                         api_mode="chat_completions"),
    "warn-capped": dict(provider="opencode-zen", base_url="https://attacker.example/v1",
                        api_mode="chat_completions"),
    "warn-retention": dict(provider="copilot", base_url="https://api.githubcopilot.com",
                           api_mode="chat_completions"),
}


def _settings(**over):
    from ipm.settings import DEFAULTS
    s = dict(DEFAULTS)
    s.update(over)
    return s


def _enforce_blocks(monkeypatch, fake_paths, settings, case, verdict):
    from ipm import enforce

    monkeypatch.setattr(enforce, "capture_paths", lambda: fake_paths)
    monkeypatch.setattr(enforce, "read_settings", lambda paths=None: dict(settings))
    monkeypatch.setattr(routes_mod, "classify", lambda *a, **k: copy.deepcopy(verdict))
    sent = []

    def next_call(req):
        sent.append(req)
        return "SENT"

    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out = enforce.on_llm_execution(request=request, next_call=next_call, session_id=None,
                                   platform="cli", model="m", **case)
    return out != "SENT"


@pytest.mark.parametrize("name", sorted(_VERDICT_CASES))
@pytest.mark.parametrize("allow_unverified", [True, False])
@pytest.mark.parametrize("block_training", [True, False])
@pytest.mark.parametrize("mode", ["private", "off"])
def test_main_label_matches_enforcement(monkeypatch, fake_paths, name, allow_unverified,
                                        block_training, mode):
    case = _VERDICT_CASES[name]
    real_classify = routes_mod.classify
    verdict = real_classify(case["provider"], case["base_url"], case["api_mode"], "m", None, None)
    settings = _settings(allow_unverified=allow_unverified, block_training=block_training, mode=mode)
    blocked = _enforce_blocks(monkeypatch, fake_paths, settings, case, verdict)
    text = routes_mod.main_label(verdict, settings)
    assert ("requests are blocked" in text) == blocked, (name, settings, text)
    assert ("requests are sent" in text) == (not blocked), (name, settings, text)
    assert "/private status detail" not in text
    assert "incognito" not in text.lower()
    if verdict.policy == "NONE_RETENTION":
        claim = text.replace("not verified", "").replace("allow_unverified", "")
        assert "verified" not in claim
        assert "retain" in text


def test_main_label_confirmed_and_needs_confirmation(monkeypatch, fake_paths):
    rec = {"k": _conf("anthropic-oauth", "api.anthropic.com", REV, auth="oauth")}
    confirmed = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth", rec)
    needs = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth", None)
    for verdict in (confirmed, needs):
        for allow in (True, False):
            settings = _settings(allow_unverified=allow)
            case = dict(provider="anthropic", base_url="https://api.anthropic.com", api_mode="chat_completions")
            blocked = _enforce_blocks(monkeypatch, fake_paths, settings, case, verdict)
            text = routes_mod.main_label(verdict, settings)
            assert ("requests are blocked" in text) == blocked, (verdict.label, allow, text)
    assert "not verified" in routes_mod.main_label(confirmed, _settings())
    assert "setup" in routes_mod.main_label(needs, _settings())


def test_coverage_lines_surfaces_and_mode_off_glyph():
    facts = {"main": {"provider": "xai", "base_url": "https://api.x.ai/v1", "model": "m"},
             "fallbacks": [], "aux": []}
    lines = routes_mod.coverage_lines(facts, None, None, _settings(apply_to_platforms=["cli", "tui"]),
                                      cleanup_active=True)
    assert "cli, tui" in lines[2] and "some copies remain" in lines[2]
    assert "CLI chats by default" not in lines[2]
    off = routes_mod.coverage_lines(facts, None, None, _settings(mode="off"), cleanup_active=False)
    assert "✅" not in off[0]
    plain = {"main": {"provider": "custom", "base_url": "http://203.0.113.9/v1", "model": "m"},
             "fallbacks": [], "aux": []}
    pl = routes_mod.coverage_lines(plain, None, None, _settings(), cleanup_active=False)
    # enforcement blocks cleartext egress before any verdict: line 1 must say so
    assert "cleartext" in pl[0].lower() and "requests are blocked" in pl[0], pl[0]
    assert "⛔" in pl[0]
    allowed = routes_mod.coverage_lines(plain, None, None, _settings(allow_plaintext_remote=True),
                                        cleanup_active=False)
    assert "cleartext" not in allowed[0].lower()
    fb_plain = {"main": facts["main"], "aux": [],
                "fallbacks": [{"provider": "custom", "base_url": "http://198.51.100.7/v1", "model": "m"}]}
    fl = routes_mod.coverage_lines(fb_plain, None, None, _settings(), cleanup_active=False)
    assert any("cleartext" in line.lower() and "198.51.100.7" in line and "blocked" in line for line in fl)


def test_coverage_line_names_the_worst_route():
    facts = {"main": {"provider": "anthropic", "base_url": "https://api.anthropic.com", "model": "c"},
             "fallbacks": [{"provider": "zai", "base_url": "https://open.bigmodel.cn/api", "model": "g"}],
             "aux": []}
    lines = routes_mod.coverage_lines(facts, None, None, _settings(), cleanup_active=False)
    assert "[worst route: fallback 1]" in lines[0] and "⛔" in lines[0]


def test_provider_only_needs_confirmation_does_not_promise_setup(syn_table):
    v = classify("poacct", None, None, None, None, None)
    assert v.label == "needs-confirmation"
    assert any("does not record confirmations" in r for r in v.reasons)


def test_confirmation_valid_never_wildcards_provider_only_rows(syn_table):
    # defense in depth under the uncurated cap: the record itself must not bind
    row = next(r for r in syn_table["rows"] if r["id"] == "po-acct")
    rec = _conf("po-acct", "", "syn-1")
    assert routes_mod.confirmation_valid(rec, row, host="", auth_hint=None, policy_rev="syn-1")
    for host in ("any.example", "localhost"):
        assert not routes_mod.confirmation_valid(rec, row, host=host, auth_hint=None, policy_rev="syn-1")
    # provider-only row + loopback: the loopback cap and the binding both refuse
    v = classify("poacct", "http://localhost:8080/v1", None, None, None, {"x": rec})
    assert v.label == "warn" and v.confirmed is False
    assert not any("you confirmed" in r for r in v.reasons)


def test_main_label_missing_keys_follow_enforcement_defaults():
    # enforce reads allow_unverified/block_training with default True
    warn = classify("deepseek", "https://api.deepseek.com", None, None, None, None)
    block = classify("zai", "https://open.bigmodel.cn/api", None, None, None, None)
    assert "requests are sent" in routes_mod.main_label(warn, {})
    assert "requests are blocked" in routes_mod.main_label(block, {})


# --- status ---------------------------------------------------------------------------

@pytest.fixture
def isolated_config():
    """run_setup / status write via save_plugin_setting into THIS worker's
    isolated HERMES_HOME config: snapshot + restore every plugin key."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import save_plugin_setting

    cfg = load_config_readonly() or {}
    before = copy.deepcopy(((cfg.get("plugins") or {}).get("entries") or {})
                          .get("incognito-private-mode", {}).get("settings") or {})
    try:
        yield before
    finally:
        from ipm.settings import DEFAULTS
        for key in DEFAULTS:
            save_plugin_setting("incognito-private-mode", (key,), before.get(key, DEFAULTS[key]))


def test_status_lists_rows_sources_and_real_surfaces(private_world, monkeypatch, isolated_config):
    from hermes_cli.plugins_state import save_plugin_setting
    from ipm import commands

    facts = {"main": {"provider": "zai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "g"},
             "fallbacks": [{"provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "d"}],
             "aux": [], "runtime": None, "memory_provider": None}
    monkeypatch.setattr(commands, "route_detection_config", lambda paths=None: facts)
    save_plugin_setting("incognito-private-mode", ("apply_to_platforms",), ["cli", "desktop"])
    save_plugin_setting("incognito-private-mode", ("keep_history",), False)
    save_plugin_setting("incognito-private-mode", ("mode",), "private")
    save_plugin_setting("incognito-private-mode", ("remove_history_on_close",), True)
    text = commands.status_text(private_world)
    assert "zhipu-cn" in text and "deepseek" in text
    for row in routes_mod.load_table()["rows"]:
        if row["id"] == "zhipu-cn":
            assert row["sources"][0]["url"] in text
    assert "cli, desktop" in text
    assert "CLI chats by default" not in text
    assert "/private status detail" not in text


def test_status_cleanup_not_enabled_when_keep_history(private_world, monkeypatch, isolated_config):
    from hermes_cli.plugins_state import save_plugin_setting
    from ipm import commands

    save_plugin_setting("incognito-private-mode", ("keep_history",), True)
    text = commands.status_text(private_world)
    assert "History cleanup: enabled" not in text
    assert "keep_history" in text


def test_status_confirmation_lines_scope(private_world, monkeypatch, isolated_config):
    from ipm import commands, store

    monkeypatch.setattr(commands, "route_detection_config", lambda paths=None: _CLAUDE_FACTS)
    scoped = {"row_id": "anthropic-oauth", "host": "api.anthropic.com", "hosts": ["api.anthropic.com"],
              "auth": "oauth", "policy_rev": REV, "profile": routes_mod.profile_scope(private_world.home)}
    store.mutate("confirmations", private_world.data_dir, lambda c: {"claude": dict(scoped)})
    text = commands.status_text(private_world)
    assert "Account confirmations (you confirmed, not verified): anthropic-oauth" in text
    assert "label confirmed" in text and "you confirmed" in text
    # legacy (unscoped) record: shown as NOT applied, and the route is not ✅
    legacy = {k: v for k, v in scoped.items() if k != "profile"}
    store.mutate("confirmations", private_world.data_dir, lambda c: {"claude": legacy,
                                                                     "openrouter": {"row_id": "openrouter"}})
    text = commands.status_text(private_world)
    assert "NOT applied" in text and "anthropic-oauth" in text
    assert "label needs-confirmation" in text
    conf_lines = [ln for ln in text.splitlines() if "onfirmations" in ln]
    assert conf_lines and not any("openrouter" in ln for ln in conf_lines)  # inert record never listed


# --- (2)/(4) setup wizard through the real write path ---------------------------------

class _Answers:
    """input() stand-in answering by prompt substring; Enter (default) otherwise."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.prompts: list[str] = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        for needle, answer in self.mapping.items():
            if needle in prompt:
                return answer
        return ""


def _run_setup(monkeypatch, paths, answers, facts=None):
    from ipm import commands

    monkeypatch.setattr(commands, "_is_tty", lambda: True)
    if facts is not None:
        monkeypatch.setattr(commands, "route_detection_config", lambda p=None: facts)
    fake_input = _Answers(answers)
    monkeypatch.setattr("builtins.input", fake_input)
    rc = commands.run_setup(paths)
    return rc, fake_input


_CLAUDE_FACTS = {"main": {"provider": "anthropic", "base_url": "https://api.anthropic.com", "model": "c"},
                 "fallbacks": [], "aux": [], "runtime": None, "memory_provider": None}

_BASE_ANSWERS = {"Profile": "3", "Mode": "private", "Cleanup surfaces": "CLI, Tui ,cli",
                 "Enable removal": "y", "Keep chats": "n"}


def test_setup_confirmation_written_and_used(monkeypatch, fake_paths, isolated_config, capsys):
    from ipm import store

    answers = dict(_BASE_ANSWERS, **{"Anthropic": "y"})
    rc, fake = _run_setup(monkeypatch, fake_paths, answers, _CLAUDE_FACTS)
    assert rc == 0
    out = capsys.readouterr().out
    # checklist asks ONLY promotable rows (Anthropic + Nebius: the Nebius row
    # gained core's real curated host, so its confirmation now binds)
    asked = [p for p in fake.prompts if "done?" in p]
    assert len(asked) == 2
    assert any("Anthropic" in a for a in asked)
    assert any("Nebius" in a for a in asked)
    assert not any(w in " ".join(asked) for w in ("OpenRouter", "Nous", "ChatGPT"))
    # informational rows are not presented as policy-changing
    assert "do not change" in out
    confs = store.read("confirmations", fake_paths.data_dir)
    rec = next(r for r in confs.values() if r.get("row_id") == "anthropic-oauth")
    assert rec["profile"] == routes_mod.profile_scope(fake_paths.home)
    assert rec["auth"] == "oauth" and rec["policy_rev"] == REV
    assert "api.anthropic.com" in rec["hosts"]
    v = classify("claude", "https://api.anthropic.com/v1", "anthropic_messages", "c", "oauth", confs,
                 profile=str(fake_paths.home))
    assert v.label == "confirmed"
    # another profile never inherits it
    other = classify("claude", "https://api.anthropic.com/v1", "anthropic_messages", "c", "oauth", confs,
                     profile=str(fake_paths.home) + "-other")
    assert other.label == "needs-confirmation"


def test_setup_answer_no_revokes_previous_and_prunes_inert(monkeypatch, fake_paths, isolated_config, capsys):
    from ipm import store

    prior = {
        "claude": {"row_id": "anthropic-oauth", "host": "api.anthropic.com", "hosts": ["api.anthropic.com"],
                   "auth": "oauth", "policy_rev": REV, "profile": routes_mod.profile_scope(fake_paths.home)},
        "openrouter": {"row_id": "openrouter", "host": "openrouter.ai", "policy_rev": REV},
        "nous": {"row_id": "nous-portal", "host": "api.nousresearch.com", "policy_rev": REV},
        "nebius": {"row_id": "nebius", "host": "", "provider": "nebius", "policy_rev": REV},
    }
    store.mutate("confirmations", fake_paths.data_dir, lambda c: dict(prior))
    before = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth",
                      store.read("confirmations", fake_paths.data_dir), profile=str(fake_paths.home))
    assert before.label == "confirmed"
    rc, _ = _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS, **{"Anthropic": "n"}), _CLAUDE_FACTS)
    assert rc == 0
    out = capsys.readouterr().out
    confs = store.read("confirmations", fake_paths.data_dir)
    assert confs == {}, confs
    assert "revoked" in out
    after = classify("anthropic", "https://api.anthropic.com", "anthropic_messages", "c", "oauth", confs,
                     profile=str(fake_paths.home))
    assert after.label == "needs-confirmation"


def test_setup_nebius_confirms_on_its_curated_host(monkeypatch, fake_paths, isolated_config, capsys):
    """Nebius gained core's real curated host (api.tokenfactory.nebius.com), so
    its account-setting confirmation now binds to that host — answering yes
    records a working confirmation; unknown hosts stay capped at warn."""
    from ipm import store
    from ipm.routes import classify

    facts = {"main": {"provider": "nebius", "base_url": "https://api.tokenfactory.nebius.com/v1", "model": "n"},
             "fallbacks": [], "aux": [], "runtime": None, "memory_provider": None}
    rc, fake = _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS, **{"Nebius": "y"}), facts)
    assert rc == 0
    out = capsys.readouterr().out
    asked = [p for p in fake.prompts if "done?" in p and "Nebius" in p]
    assert asked  # Nebius is asked now that its row carries a curated host
    assert "api.tokenfactory.nebius.com" in out  # summary shows the bound host
    confs = store.read("confirmations", fake_paths.data_dir)
    rec = next(r for r in confs.values() if r.get("row_id") == "nebius")
    assert rec["hosts"] == ["api.tokenfactory.nebius.com"]
    v = classify("nebius", "https://api.tokenfactory.nebius.com/v1", None, "n", None, confs,
                 profile=str(fake_paths.home))
    assert v.label == "confirmed"
    # a different host is NOT blessed by the confirmation (round-#3 Astra F6)
    other = classify("nebius", "https://untrusted.invalid/v1", None, "n", None, confs,
                     profile=str(fake_paths.home))
    assert other.label in {"warn", "needs-confirmation"}
    assert other.label != "confirmed"


def test_setup_surfaces_lowercased_and_summary_enabled(monkeypatch, fake_paths, isolated_config, capsys):
    from ipm.settings import read_settings

    rc, _ = _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS))
    assert rc == 0
    out = capsys.readouterr().out
    assert read_settings(fake_paths)["apply_to_platforms"] == ["cli", "tui"]
    assert "cleanup=ENABLED" in out


@pytest.mark.parametrize("override,reason", [
    ({"Keep chats": "y"}, "keep_history"),
    ({"Cleanup surfaces": "none"}, "no cleanup surfaces"),
    ({"Mode": "off"}, "mode is off"),
    ({"Enable removal": "n"}, "no consent"),
])
def test_setup_summary_not_enabled(monkeypatch, fake_paths, isolated_config, capsys, override, reason):
    rc, _ = _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS, **override))
    assert rc == 0
    out = capsys.readouterr().out
    assert "cleanup=ENABLED" not in out
    assert reason in out


def test_setup_consent_text_honest(monkeypatch, fake_paths, isolated_config, capsys):
    _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS))
    out = capsys.readouterr().out
    consent = out[out.index("History removal consent"):out.index("Detected routes")]
    assert "compress" in consent and "never removed" in consent
    assert "/new" in consent and "not proven" in consent
    assert "only where the whole lineage closed cleanly" not in consent


def test_setup_intro_qualified_when_mode_off(monkeypatch, fake_paths, isolated_config, capsys):
    from hermes_cli.plugins_state import save_plugin_setting

    save_plugin_setting("incognito-private-mode", ("mode",), "off")
    _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS))
    out = capsys.readouterr().out
    intro = out[:out.index("Pick a starting point")]
    assert "already active" not in intro
    assert "OFF" in intro and "not applied" in intro


def test_setup_intro_when_mode_private(monkeypatch, fake_paths, isolated_config, capsys):
    from hermes_cli.plugins_state import save_plugin_setting

    save_plugin_setting("incognito-private-mode", ("mode",), "private")
    _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS))
    out = capsys.readouterr().out
    intro = out[:out.index("Pick a starting point")]
    assert "mode=private" in intro


def test_setup_non_tty_writes_nothing(monkeypatch, fake_paths, isolated_config):
    from ipm import commands, store

    monkeypatch.setattr(commands, "_is_tty", lambda: False)
    assert commands.run_setup(fake_paths) == 2
    assert store.read("confirmations", fake_paths.data_dir) == {}
    assert store.read("meta", fake_paths.data_dir) == {}


def test_confirmations_file_is_json_dict(monkeypatch, fake_paths, isolated_config):
    _run_setup(monkeypatch, fake_paths, dict(_BASE_ANSWERS, **{"Anthropic": "y"}), _CLAUDE_FACTS)
    raw = (fake_paths.data_dir / "confirmations.json").read_text()
    assert isinstance(json.loads(raw), dict)
