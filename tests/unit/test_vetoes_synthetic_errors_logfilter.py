"""Unit: vetoes, synthetic shapes, error classification, log filter (§6 row 2)."""

import logging

from ipm import errors, synthetic, vetoes
from ipm.settings import DEFAULTS


def test_veto_table_defaults():
    settings = dict(DEFAULTS)
    assert vetoes.veto_reason("memory", settings, None, None) == \
        "Blocked by private mode: memory saving is off for this chat."
    assert vetoes.veto_reason("skill_manage", settings, None, None) == \
        "Blocked by private mode: skill learning is off for this chat."
    assert vetoes.veto_reason("session_search", settings, None, None) == \
        "Blocked by private mode: past-chat search is off for this chat."
    assert vetoes.veto_reason("terminal", settings, None, None) is None


def test_veto_toggles_allow():
    settings = dict(DEFAULTS, save_memory=True, learn_skills=True, keep_history=True)
    assert vetoes.veto_reason("memory", settings, None, None) is None
    assert vetoes.veto_reason("skill_manage", settings, None, None) is None
    assert vetoes.veto_reason("session_search", settings, None, None) is None


def test_veto_mode_off_allows():
    settings = dict(DEFAULTS, mode="off")
    assert vetoes.veto_reason("memory", settings, None, None) is None
    # /private on forces private even in mode off
    assert vetoes.veto_reason("memory", settings, {"override": "on"}, None) is not None


def test_veto_external_provider_tool():
    settings = dict(DEFAULTS)
    assert vetoes.veto_reason("supermemory", settings, None, "supermemory") is not None
    assert vetoes.veto_reason("unrelated", settings, None, "supermemory") is None


def test_synthetic_chat_completions():
    response = synthetic.block_response("chat_completions", "blocked")
    from openai.types.chat import ChatCompletion

    assert isinstance(response, ChatCompletion)
    assert response.choices[0].message.content == "blocked"
    assert response.choices[0].finish_reason == "stop"


def test_synthetic_codex_responses():
    response = synthetic.block_response("codex_responses", "blocked")
    from openai.types.responses import Response

    assert isinstance(response, Response)
    texts = [part.text for item in response.output
             for part in getattr(item, "content", [])]
    assert "blocked" in texts


def test_synthetic_anthropic_probe_shape():
    response = synthetic.block_response("anthropic_messages", "blocked")
    assert response.stop_reason == "end_turn"
    assert response.usage is None
    assert response.content[0].type == "text"
    assert response.content[0].text == "blocked"


def test_synthetic_unknown_mode_none():
    assert synthetic.block_response("bedrock_converse", "blocked") is None
    assert synthetic.block_response(None, "blocked") is None


def test_error_classification_no_private_endpoint():
    kwargs = {"provider": "openrouter", "error_message":
              "No endpoints found matching your data policy"}
    result = errors.classify_error(kwargs, mode_active=True, provider="openrouter")
    assert result is not None
    assert result["reason"] == "provider_policy_blocked"
    assert result["retryable"] is False
    assert "Private mode" in result["message"]
    # off -> no opinion; unrelated errors -> no opinion
    assert errors.classify_error(kwargs, mode_active=False, provider="openrouter") is None
    assert errors.classify_error({"error_message": "rate limited"}, mode_active=True,
                                 provider="openrouter") is None


class _Capture:
    def __init__(self):
        self.records = []


def test_logfilter_redacts_gateway_inbound_args():
    from ipm.logfilter import PrivateLogFilter

    flt = PrivateLogFilter(lambda: True)
    record = logging.LogRecord("gateway.run", logging.INFO, "p", 1,
                               "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
                               ("telegram", "u1", "c1", "SECRET TEXT", 42, "secret reply"), None)
    assert flt.filter(record) is True
    assert "[redacted]" in str(record.args)
    assert "SECRET TEXT" not in str(record.args)
    assert "secret reply" not in str(record.args)
    assert flt.redacted == 1


def test_logfilter_redacts_turn_line():
    from ipm.logfilter import PrivateLogFilter

    flt = PrivateLogFilter(lambda: True)
    record = logging.LogRecord("agent.turn_context", logging.INFO, "p", 1,
                               "conversation turn: session=%s model=%s provider=%s platform=%s history=%d msg=%r",
                               ("s1", "m", "p", "cli", 3, "SECRET INPUT"), None)
    flt.filter(record)
    assert "SECRET INPUT" not in str(record.args)


def test_logfilter_redacts_formatted_fstring():
    from ipm.logfilter import PrivateLogFilter

    flt = PrivateLogFilter(lambda: True)
    record = logging.LogRecord("gateway.run", logging.INFO, "p", 1,
                               f"inbound message: platform=t msg={'sentinel-text!'} x=1", None, None)
    flt.filter(record)
    assert "sentinel-text!" not in record.getMessage()


def test_logfilter_inactive_leaves_records():
    from ipm.logfilter import PrivateLogFilter

    flt = PrivateLogFilter(lambda: False)
    record = logging.LogRecord("gateway.run", logging.INFO, "p", 1,
                               "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
                               ("t", "u", "c", "visible", None, None), None)
    flt.filter(record)
    assert "visible" in str(record.args)


def test_attach_detach_roundtrip():
    from ipm import logfilter

    logfilter.attach(lambda: True)
    lg = logging.getLogger("gateway.run")
    assert any(getattr(f, "_ipm_private_filter", False) for f in lg.filters)
    logfilter.detach()
    assert not any(getattr(f, "_ipm_private_filter", False) for f in lg.filters)
