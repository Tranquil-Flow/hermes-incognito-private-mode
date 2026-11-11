"""Typed synthetic block responses per api_mode (BUILD_SPEC §3.1).

chat_completions -> openai.types.chat.ChatCompletion
codex_responses  -> openai.types.responses.Response
anthropic_messages -> SimpleNamespace (probe-verified shape)
others           -> None (fail closed: Hermes retries then errors out — poor UX,
never a leak; a synthetic stop does not trigger fallback).
"""

from __future__ import annotations

import logging
import time
import uuid
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("ipm.synthetic")


def _openai():
    try:
        import openai  # noqa: F401  (Hermes-provided SDK type; not an extra dependency)
        return openai
    except Exception:
        return None


def block_response(api_mode: str | None, text: str) -> Any:
    """Return a provider-shaped terminal response carrying *text*, or None."""
    if not text:
        text = "Blocked by private mode."
    try:
        if api_mode == "chat_completions":
            return _chat_completion(text)
        if api_mode == "codex_responses":
            return _responses_response(text)
        if api_mode == "anthropic_messages":
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn", usage=None,
            )
    except Exception as exc:  # fail closed
        logger.debug("ipm: synthetic construction failed (%s)", type(exc).__name__)
    return block_response_dict(api_mode, text)


def block_response_dict(api_mode: str | None, text: str) -> Any:
    """SDK-independent dict fallback for the three known modes. A plain dict is
    serializable by every OpenAI/Anthropic transport, so an internal-error
    block can be delivered even when SDK construction fails (round-#3 Astra
    F5: shaping failure must never fall through to the provider)."""
    if not text:
        text = "Blocked by private mode."
    if api_mode == "chat_completions":
        return {
            "id": f"ipm-{uuid.uuid4().hex[:24]}", "object": "chat.completion",
            "created": int(time.time()), "model": "private-mode-block",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
        }
    if api_mode == "codex_responses":
        return {
            "id": f"resp_{uuid.uuid4().hex[:24]}", "object": "response",
            "created_at": time.time(), "model": "private-mode-block",
            "status": "completed", "parallel_tool_calls": False,
            "tool_choice": "none", "tools": [],
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "id": f"msg_{uuid.uuid4().hex[:24]}",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        }
    if api_mode == "bedrock_converse":
        # core's bedrock terminal returns a ChatCompletion-shaped object
        # (normalize_converse_response), so the chat shape is the correct
        # terminal block here (round-#4 Opus F5) — no content-free scrub path.
        return block_response_dict("chat_completions", text)
    if api_mode == "anthropic_messages":
        return {"role": "assistant", "stop_reason": "end_turn", "usage": None,
                "content": [{"type": "text", "text": text}]}
    if api_mode == "bedrock_converse":
        # core's bedrock terminal returns a ChatCompletion-shaped object
        # (normalize_converse_response), so the chat dict shape is the correct
        # terminal block (round-#4 Opus F5) — no content-free scrub path.
        return block_response_dict("chat_completions", text)
    return None


def _chat_completion(text: str) -> Any:
    openai = _openai()
    if openai is None:
        return None
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice

    return ChatCompletion(
        id=f"ipm-{uuid.uuid4().hex[:24]}",
        choices=[Choice(finish_reason="stop", index=0,
                        message=ChatCompletionMessage(role="assistant", content=text))],
        created=int(time.time()),
        model="private-mode-block",
        object="chat.completion",
    )


def _responses_response(text: str) -> Any:
    openai = _openai()
    if openai is None:
        return None
    from openai.types.responses import Response
    from openai.types.responses.response_output_message import ResponseOutputMessage
    from openai.types.responses.response_output_text import ResponseOutputText

    msg = ResponseOutputMessage(
        id=f"msg_{uuid.uuid4().hex[:24]}",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant", status="completed", type="message",
    )
    return Response(
        id=f"resp_{uuid.uuid4().hex[:24]}",
        created_at=time.time(),
        model="private-mode-block",
        object="response",
        output=[msg],
        parallel_tool_calls=False,
        tool_choice="none",
        tools=[],
    )
