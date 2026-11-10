"""Wire-level memory strip (BUILD_SPEC §3.5).

Removes, from the outgoing request copy only:
- recognised built-in memory blocks (``tools/memory_tool_store.py`` ``_render_block``
  format: a 46-``═`` rule, the block header, another rule, the entries) from
  chat_completions ``messages[0]`` system text (role ``system`` OR ``developer``),
  codex ``instructions`` and anthropic ``system``;
- ``<memory-context>…</memory-context>`` spans (``agent/memory_context.py``) from
  user messages.

Block-span strategy (fixed after the blank-line-entry audit finding):
1. PRIMARY — exact reconstruction. The plugin renders the block from the same
   entries core renders (``'\\n§\\n'.join(entries)`` under a rule/title/rule
   header) and removes that exact substring. Core builds the prompt from its
   load-time snapshot of these files, so an unchanged file yields an exact match.
2. FALLBACK — if reconstruction is unavailable or does not match, strip the
   header + entries up to the first blank line and raise an ``ambiguous`` flag.
3. FAIL CLOSED — the caller (enforce) treats ``ambiguous`` as memory-still-present
   and blocks the request rather than risk sending entry content. Blank lines
   inside entries are legal in core (``MemoryStore.add`` only ``strip()``s the
   ends), so a heuristic end boundary alone is NOT trustworthy.

Claim wording: "recognised memory blocks are removed from main-chat requests" —
NOT "memory is never read".
"""

from __future__ import annotations

import re
from typing import Any

MEMORY_TITLE_RE = re.compile(
    r"^(MEMORY \(your personal notes\)|USER PROFILE \(who the user is\))\s*\[\d+%[^]]*]\s*$")
RULE_RE = re.compile(r"^\u2550{46}\s*$")
MEMORY_CONTEXT_OPEN = "<memory-context>"
MEMORY_CONTEXT_CLOSE = "</memory-context>"
# Core's own scrubber is tag-tolerant: `<\s*memory-context\s*>`, case-insensitive.
_MEMORY_TAG_OPEN_RE = re.compile(r"<\s*memory-context\s*>", re.IGNORECASE)
_MEMORY_TAG_CLOSE_RE = re.compile(r"<\s*/\s*memory-context\s*>", re.IGNORECASE)

_ENTRY_DELIMITER = "\n§\n"

# Bodies of the memory blocks as core would render them, fed by enforce from the
# captured memory files (mtime-cached there). Each element is the joined entry
# body ('entry1\n§\nentry2').
_memory_bodies: list[str] = []


def set_memory_bodies(bodies: list[str]) -> None:
    """Feed candidate block bodies (exact-reconstruction sources)."""
    global _memory_bodies
    try:
        _memory_bodies = [b for b in (bodies or []) if isinstance(b, str) and b]
    except Exception:
        _memory_bodies = []


def reset_memory_bodies() -> None:
    global _memory_bodies
    _memory_bodies = []


def _render_candidates(title_line: str) -> list[str]:
    """Exact block texts to search for, given the concrete header line seen in
    the prompt. Percent/char formatting is taken from the prompt itself, so we
    only need the body to match."""
    rule = "═" * 46
    out: list[str] = []
    for body in _memory_bodies:
        out.append(f"{rule}\n{title_line}\n{rule}\n{body}")
    return out


def has_memory_block(text: str) -> bool:
    if not isinstance(text, str):
        return False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if MEMORY_TITLE_RE.match(line.strip()) and i > 0 and RULE_RE.match(lines[i - 1].strip()):
            return True
    return False


def _remove_span(text: str, start: int, end: int) -> str:
    """Remove text[start:end] and repair the junction boundary-locally."""
    before = text[:start]
    after = text[end:]
    if not before:
        return after.lstrip("\n")
    if not after:
        return before.rstrip("\n")
    return before.rstrip("\n") + "\n\n" + after.lstrip("\n")


def strip_memory_blocks(text: str) -> tuple[str, int]:
    """Compat wrapper: returns (text, count) without the ambiguity flag."""
    out, count, _amb = strip_memory_blocks_checked(text)
    return out, count


def strip_memory_blocks_checked(text: str) -> tuple[str, int, bool]:
    """Remove whole blocks; returns (text, count, ambiguous).

    ``ambiguous=True`` means a memory header was present and the entries may
    NOT have been fully removed (blank lines inside entries make a purely
    textual end boundary untrustworthy). The caller must fail closed.
    """
    if not isinstance(text, str) or not text:
        return text, 0, False
    lines = text.split("\n")
    starts: list[int] = []  # index of the rule line ABOVE each header
    for i, line in enumerate(lines):
        if i > 0 and MEMORY_TITLE_RE.match(line.strip()) and RULE_RE.match(lines[i - 1].strip()):
            starts.append(i - 1)
    if not starts:
        return text, 0, False

    ambiguous = False
    removed = 0
    result = text
    # Process from the last block backwards so indices stay valid.
    for start in reversed(starts):
        title_line = lines[start + 1].strip()
        rule = "═" * 46
        header_end = result.find(f"{rule}\n{title_line}\n{rule}\n")
        if header_end == -1:
            continue
        body_start = header_end + len(f"{rule}\n{title_line}\n{rule}\n")
        span_end = -1
        # PRIMARY: exact reconstruction from the fed bodies.
        for cand in _render_candidates(title_line):
            idx = result.find(cand, header_end)
            if idx == header_end:
                span_end = header_end + len(cand)
                break
        if span_end == -1:
            # FALLBACK: entries up to the first blank line; flag ambiguity
            # (core entries MAY contain blank lines).
            sub_lines = lines  # line indices align with the ORIGINAL text only
            # recompute in result-space: walk lines of result from body_start
            rlines = result.split("\n")
            # find body_start line index
            pos = 0
            bi = 0
            for bi, ln in enumerate(rlines):
                if pos + len(ln) + 1 > body_start:
                    break
                pos += len(ln) + 1
            end_i = len(rlines)
            for j in range(bi + 1, len(rlines)):
                if rlines[j].strip() == "":
                    end_i = j
                    break
                if RULE_RE.match(rlines[j].strip()):
                    end_i = j
                    break
            span_end = len("\n".join(rlines[:end_i])) if end_i else 0
            if end_i < len(rlines):
                span_end += 1  # the newline joining end_i-1 and end_i
            ambiguous = True
        result = _remove_span(result, header_end, span_end)
        removed += 1
    return result, removed, ambiguous


def _find_well_ordered_span(text: str, from_pos: int = 0) -> tuple[int, int] | None:
    """First well-ordered tolerant-tag span at/after from_pos, or None.
    Linear scan — no backtracking regex (ReDoS-safe)."""
    o = _MEMORY_TAG_OPEN_RE.search(text, from_pos)
    if not o:
        return None
    c = _MEMORY_TAG_CLOSE_RE.search(text, o.end())
    if not c:
        return None  # unpaired open: caller decides (fail closed)
    return o.start(), c.end()


def has_memory_context_span(text: str) -> bool:
    """True only for a WELL-ORDERED tolerant-tag span (open before close).
    Unpaired or reversed tags do NOT count as a strippable span; the caller
    decides fail-closed handling via span_residue()."""
    if not isinstance(text, str):
        return False
    return _find_well_ordered_span(text) is not None


def span_residue(text: str) -> bool:
    """True when a tolerant tag survives with no strippable well-ordered span
    (unpaired open, reversed order, or a bare close) — i.e. strip cannot fully
    clean this text."""
    if not isinstance(text, str):
        return False
    o = _MEMORY_TAG_OPEN_RE.search(text)
    if not o:
        return bool(_MEMORY_TAG_CLOSE_RE.search(text))
    c = _MEMORY_TAG_CLOSE_RE.search(text, o.end())
    if not c:
        return True  # open without close: cannot strip
    return False


def strip_memory_context_spans(text: str) -> tuple[str, int]:
    """Remove well-ordered spans (linear scan). Returns (text, count)."""
    if not isinstance(text, str) or not text:
        return text, 0
    count = 0
    pos = 0
    while True:
        span = _find_well_ordered_span(text, pos)
        if span is None:
            break
        start, end = span
        text = _remove_span(text, start, end)
        count += 1
        pos = max(0, start - 0)  # continue scanning from the junction
        if count > 256:  # paranoia bound (linear scan makes this unreachable in practice)
            break
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


def is_system_message(msg: Any, index: int) -> bool:
    """The system prompt occupies messages[0]; GPT-5/Codex transports may swap
    its role to 'developer' before middleware runs."""
    return index == 0 and isinstance(msg, dict) and msg.get("role") in ("system", "developer")


def strip_request(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Strip a full request (chat_completions / codex_responses / anthropic_messages
    shapes). Copy-on-write: returns the ORIGINAL object when nothing was removed.
    Counts: {blocks, spans, ambiguous}."""
    counts = {"blocks": 0, "spans": 0, "ambiguous": 0}
    new_request = request
    copied = False

    def ensure_copy() -> dict[str, Any]:
        nonlocal new_request, copied
        if not copied:
            new_request = dict(request)
            copied = True
        return new_request

    def strip_system(text: str) -> str:
        text2, n, amb = strip_memory_blocks_checked(text)
        counts["blocks"] += n
        if amb:
            counts["ambiguous"] += 1
        return text2

    # --- chat_completions (also anthropic 'messages'): system = messages[0];
    #     spans in user messages
    messages = request.get("messages")
    if isinstance(messages, list):
        new_messages: list[Any] | None = None
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            is_system = is_system_message(msg, i)
            texts = content_texts(msg.get("content"))
            changed = False
            new_texts: list[Any] = []
            for text in texts:
                text2 = strip_system(text) if is_system else text
                if not is_user_message(msg):
                    new_texts.append(text2)
                    if text2 != text:
                        changed = True
                    continue
                # user message: strip well-ordered spans; fail closed on residue
                text3, s = strip_memory_context_spans(text2 if isinstance(text2, str) else text)
                counts["spans"] += s
                if isinstance(text3, str) and span_residue(text3):
                    counts["ambiguous"] += 1
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
        text2 = strip_system(instructions)
        if text2 != instructions:
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
                if isinstance(text2, str) and span_residue(text2):
                    counts["ambiguous"] += 1
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

    # --- anthropic_messages: system (str or block list); user spans already
    # handled by the shared `messages` pass above (single pass, no double-count).
    system = request.get("system")
    if isinstance(system, str):
        text2 = strip_system(system)
        if text2 != system:
            ensure_copy()["system"] = text2
    elif isinstance(system, list):
        new_system: list[Any] | None = None
        for i, part in enumerate(system):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text2 = strip_system(part["text"])
                if text2 != part["text"]:
                    new_system = list(system) if new_system is None else new_system
                    new_system[i] = {**part, "text": text2}
        if new_system is not None:
            ensure_copy()["system"] = new_system

    return new_request, counts
