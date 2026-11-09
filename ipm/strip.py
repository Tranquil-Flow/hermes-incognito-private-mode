"""Wire-level memory strip (BUILD_SPEC §3.5).

Removes, from the outgoing request copy only:
- recognised built-in memory blocks (``tools/memory_tool_store.py`` ``_render_block``
  format: a 46-``═`` rule, the block header, another rule, the entries) from
  chat_completions ``messages[0]`` system text, codex ``instructions`` and
  anthropic ``system``;
- ``<memory-context>…</memory-context>`` spans (``agent/memory_context.py``) from
  user messages.

Claim wording: "recognised memory blocks are removed from main-chat requests" —
NOT "memory is never read".
"""

from __future__ import annotations

import re
from typing import Any

MEMORY_TITLE_RE = re.compile(
    r"^(MEMORY \(your personal notes\)|USER PROFILE \(who the user is\)) \[\d+%[^]]*]\s*$")
RULE_RE = re.compile(r"^\u2550{46}\s*$")
MEMORY_CONTEXT_OPEN = "<memory-context>"
MEMORY_CONTEXT_CLOSE = "</memory-context>"
_MEMORY_CONTEXT_RE = re.compile(re.escape(MEMORY_CONTEXT_OPEN) + r".*?" + re.escape(MEMORY_CONTEXT_CLOSE),
                                re.DOTALL)


def has_memory_block(text: str) -> bool:
    if not isinstance(text, str):
        return False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if MEMORY_TITLE_RE.match(line.strip()) and i > 0 and RULE_RE.match(lines[i - 1].strip()):
            return True
    return False


def strip_memory_blocks(text: str) -> tuple[str, int]:
    """Remove whole blocks (rule+header+rule+entries) from *text*; returns (text, count)."""
    if not isinstance(text, str) or not text:
        return text, 0
    lines = text.split("\n")
    # Block starts: index of the rule line directly above a header line.
    starts: list[int] = []
    for i, line in enumerate(lines):
        if i > 0 and MEMORY_TITLE_RE.match(line.strip()) and RULE_RE.match(lines[i - 1].strip()):
            starts.append(i - 1)
    if not starts:
        return text, 0
    # A block runs from its first rule through the entries until the next block's
    # first rule (which sits right above the next header) or end of text.
    ranges: list[tuple[int, int]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        ranges.append((start, end))
    keep: list[str] = []
    skip_ranges = set()
    for start, end in ranges:
        for i in range(start, end):
            skip_ranges.add(i)
    for i, line in enumerate(lines):
        if i in skip_ranges:
            continue
        keep.append(line)
    out = "\n".join(keep)
    out = re.sub(r"\n{3,}", "\n\n", out).strip("\n")
    return out, len(ranges)


def has_memory_context_span(text: str) -> bool:
    return isinstance(text, str) and MEMORY_CONTEXT_OPEN in text and MEMORY_CONTEXT_CLOSE in text


def strip_memory_context_spans(text: str) -> tuple[str, int]:
    if not isinstance(text, str) or not text:
        return text, 0
    count = 0
    while has_memory_context_span(text):
        text = _MEMORY_CONTEXT_RE.sub("", text, count=1)
        count += 1
        if count > 64:  # paranoia bound
            break
    if count:
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text, count


def message_texts(messages: list[Any]) -> list[str]:
    """Every text-bearing string in chat_completions ``messages`` entries."""
    out: list[str] = []
    for msg in messages:
        if isinstance(msg, dict):
            out.extend(content_texts(msg.get("content")))
    return out


def content_texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return out
    return []


def anthropic_system_texts(system: Any) -> list[str]:
    if isinstance(system, str):
        return [system]
    return content_texts(system)


def is_user_message(msg: Any) -> bool:
    return isinstance(msg, dict) and msg.get("role") == "user"


def strip_request(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Strip a full request (chat_completions / codex_responses / anthropic_messages
    shapes). Copy-on-write: returns the ORIGINAL object when nothing was removed.
    Counts: {blocks, spans}."""
    counts = {"blocks": 0, "spans": 0}
    new_request = request
    copied = False

    def ensure_copy() -> dict[str, Any]:
        nonlocal new_request, copied
        if not copied:
            new_request = dict(request)
            copied = True
        return new_request

    # --- chat_completions: system = messages[0] (role system); spans in user messages
    messages = request.get("messages")
    if isinstance(messages, list):
        new_messages: list[Any] | None = None
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            is_system = i == 0 and msg.get("role") == "system"
            texts = content_texts(msg.get("content"))
            changed = False
            new_texts: list[Any] = []
            for text in texts:
                if is_system:
                    text2, n = strip_memory_blocks(text)
                    counts["blocks"] += n
                else:
                    text2 = text
                if not is_user_message(msg):
                    new_texts.append(text2)
                    if text2 != text:
                        changed = True
                    continue
                text3, s = strip_memory_context_spans(text2 if isinstance(text2, str) else text)
                counts["spans"] += s
                new_texts.append(text3)
                if text3 != (text2 if isinstance(text2, str) else text):
                    changed = True
            if changed and isinstance(msg.get("content"), str):
                new_messages = list(messages) if new_messages is None else new_messages
                new_messages[i] = {**msg, "content": new_texts[0]}
            elif changed:
                new_messages = list(messages) if new_messages is None else new_messages
                parts = msg["content"]
                new_parts: list[Any] = []
                ti = 0
                for part in parts:
                    if isinstance(part, str) or (isinstance(part, dict) and isinstance(part.get("text"), str)):
                        new_parts.append({**part, "text": new_texts[ti]} if isinstance(part, dict) else new_texts[ti])
                        ti += 1
                    else:
                        new_parts.append(part)
                new_messages[i] = {**msg, "content": new_parts}
        if new_messages is not None:
            ensure_copy()["messages"] = new_messages

    # --- codex_responses: instructions + input items
    instructions = request.get("instructions")
    if isinstance(instructions, str):
        text2, n = strip_memory_blocks(instructions)
        if n:
            counts["blocks"] += n
            ensure_copy()["instructions"] = text2
    inp = request.get("input")
    if isinstance(inp, list):
        new_input: list[Any] | None = None
        for i, item in enumerate(inp):
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            texts = content_texts(item.get("content"))
            changed = False
            span_texts: list[str] = []
            for text in texts:
                text2, s = strip_memory_context_spans(text)
                counts["spans"] += s
                span_texts.append(text2)
                if text2 != text:
                    changed = True
            if changed:
                new_input = list(inp) if new_input is None else new_input
                if isinstance(item.get("content"), str):
                    new_input[i] = {**item, "content": span_texts[0]}
                else:
                    parts = item["content"]
                    new_parts: list[Any] = []
                    ti = 0
                    for part in parts:
                        if isinstance(part, str):
                            new_parts.append(span_texts[ti]); ti += 1
                        elif isinstance(part, dict) and isinstance(part.get("text"), str):
                            new_parts.append({**part, "text": span_texts[ti]}); ti += 1
                        else:
                            new_parts.append(part)
                    new_input[i] = {**item, "content": new_parts}
        if new_input is not None:
            ensure_copy()["input"] = new_input

    # --- anthropic_messages: system (str or block list) + user message content
    system = request.get("system")
    if isinstance(system, str):
        text2, n = strip_memory_blocks(system)
        if n:
            counts["blocks"] += n
            ensure_copy()["system"] = text2
    elif isinstance(system, list):
        new_system: list[Any] | None = None
        for i, part in enumerate(system):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text2, n = strip_memory_blocks(part["text"])
                if n:
                    counts["blocks"] += n
                    new_system = list(system) if new_system is None else new_system
                    new_system[i] = {**part, "text": text2}
        if new_system is not None:
            ensure_copy()["system"] = new_system
    messages = request.get("messages")
    if isinstance(messages, list):
        new_messages = None
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            for j, part in enumerate(msg.get("content") or []):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text2, s = strip_memory_context_spans(part["text"])
                    if s:
                        counts["spans"] += s
                        new_messages = list(messages) if new_messages is None else new_messages
                        new_messages[i] = {
                            **msg,
                            "content": [
                                ({**p, "text": text2} if k == j else p)
                                for k, p in enumerate(msg["content"])
                            ],
                        }
        if new_messages is not None:
            ensure_copy()["messages"] = new_messages

    return new_request, counts
