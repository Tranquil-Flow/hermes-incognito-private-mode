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

# Module-global bodies kept ONLY for the legacy set/reset API; enforce passes
# bodies as an argument per call (the global was a cross-thread race: one
# thread's reset emptied it while another was mid-strip — round-#3 review).
_memory_bodies: list[str] = []


def set_memory_bodies(bodies: list[str]) -> None:
    """Legacy feed for candidate block bodies. Prefer the bodies= argument."""
    global _memory_bodies
    try:
        _memory_bodies = [b for b in (bodies or []) if isinstance(b, str) and b]
    except Exception:
        _memory_bodies = []


def reset_memory_bodies() -> None:
    global _memory_bodies
    _memory_bodies = []


# Core's header usage line carries the EXACT snapshot body length:
# "NN% — X/Y chars" where X == len(snapshot body) (memory_tool_store.py:644).
_USAGE_LEN_RE = re.compile(r"\u2014\s*([\d,]+)\s*/")


def _snapshot_len(title_line: str) -> int | None:
    """Exact snapshot body length from the header's usage figure, or None."""
    m = _USAGE_LEN_RE.search(title_line)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _render_candidates(title_line: str, bodies: list[str] | None = None) -> list[str]:
    """Exact block texts to search for, given the concrete header line seen in
    the prompt. Percent/char formatting is taken from the prompt itself, so we
    only need the body to match."""
    rule = "═" * 46
    src_bodies = bodies if bodies is not None else _memory_bodies
    out: list[str] = []
    for body in src_bodies:
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


def strip_memory_blocks_checked(text: str, bodies: list[str] | None = None) -> tuple[str, int, bool]:
    """Remove whole blocks; returns (text, count, ambiguous).

    ``bodies``: exact-reconstruction candidates from the captured memory files
    (per-call argument — never shared mutable state across threads).

    ``ambiguous=True`` means a memory header was present and the body could not
    be proven fully removed. The caller must fail closed.
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

    # Plan spans against the ORIGINAL text (never the already-edited result):
    # nested/later headers would otherwise be measured with stale offsets and
    # over-consume neighbouring system text (round-#3 Astra F3.B).
    result = text
    ambiguous = False
    removed = 0
    accepted: list[tuple[int, int]] = []
    for start in starts:  # ascending: outer blocks precede nested ones
        title_line = lines[start + 1].strip()
        rule = "\u2550" * 46
        header = f"{rule}\n{title_line}\n{rule}\n"
        header_start = text.find(header, max(accepted[-1][1] if accepted else 0, 0))
        # locate this block's header at/after the previous accepted span
        search_from = 0
        header_start = -1
        while True:
            header_start = text.find(header, search_from)
            if header_start == -1:
                break
            span = (header_start, header_start + len(header))
            if not any(s <= span[0] < e for s, e in accepted):
                break
            search_from = header_start + 1
        if header_start == -1:
            continue
        body_start = header_start + len(header)
        body_end = -1
        # PRIMARY: header-length snapshot, but ONLY when the sliced body
        # matches a fed candidate (proof of authentic core framing). A prompt
        # supplied length alone cannot prove complete removal: a forged short
        # header would otherwise forward the remainder (round-#3 Astra F3.C).
        snap_len = _snapshot_len(title_line)
        if snap_len is not None:
            end = body_start + snap_len
            if end <= len(text) and (end == len(text) or text[end] == "\n"):
                body_slice = text[body_start:end]
                if body_slice in (bodies if bodies is not None else _memory_bodies):
                    body_end = end
        # SECONDARY: exact reconstruction from fed bodies (cleaned + raw).
        # Candidates are FULL blocks (header+body), so they anchor at the
        # header start, not the body start. Round-#4 Astra F1: when the title
        # advertises a snapshot length, a candidate match is ONLY proof if it
        # consumed exactly that span — a truncated file would otherwise match
        # as a proper PREFIX, strip the identifying header and leak the frozen
        # tail invisibly. Length-advertised blocks therefore rely solely on
        # the primary path; candidates apply only to legacy headers without
        # a usable length.
        #
        # Known boundary (round-#5 self-review, integrity-only): a header
        # FORGED to advertise an observed version's length, with a body of
        # that version plus attacker-chosen extra lines, strips the proven
        # span and forwards the extra lines unflagged — "═ frame \n
        # non-blank continuation" is indistinguishable from a legitimate
        # neighbouring text line. The extra lines are attacker-authored (the
        # forger already knows the matched body), so this is spoofing, not a
        # confidentiality leak; core writes TRUE lengths, so honest snapshots
        # are unaffected. History candidates are exact full-body matches of
        # real observed versions, so they add no new exposure here.
        if body_end == -1 and snap_len is None:
            for cand in _render_candidates(title_line, bodies):
                if text.startswith(cand, header_start):
                    body_end = header_start + len(cand)
                    break
        if body_end == -1:
            # UNPROVABLE: keep the block verbatim; ambiguity flags it. A
            # partial fallback strip would both leak the tail and destroy the
            # header the final recheck needs (round-#3 Astra F3).
            ambiguous = True
            continue
        accepted.append((header_start, body_end))
        removed += 1
    # Remove accepted spans descending (non-overlapping by construction).
    for header_start, body_end in sorted(accepted, reverse=True):
        result = _remove_span(result, header_start, body_end)
    return result, removed, ambiguous


def memory_title_residue(text: str) -> bool:
    """True when a recognizable memory-title line exists WITHOUT its Unicode
    rule framing (or with the framing stripped by core's ASCII-retry
    sanitization). Such text may carry memory content that the rule-anchored
    stripper cannot see — callers must treat it as ambiguous and block
    (round-#3 Astra F3.A)."""
    if not isinstance(text, str) or not text:
        return False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if MEMORY_TITLE_RE.match(line.strip()):
            if i == 0 or not RULE_RE.match(lines[i - 1].strip()):
                return True
    return False


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


def strip_request(request: dict[str, Any], bodies: list[str] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
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
        text2, n, amb = strip_memory_blocks_checked(text, bodies)
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


_PERSONA_PLACEHOLDER = "[persona omitted: private mode]"


def replace_persona_span(request: dict[str, Any], persona_text: str) -> tuple[dict[str, Any], int]:
    """Best-effort removal of the user's SOUL.md persona from the request.

    Exact-substring replacement only: the persona text as the loader renders it
    (stripped) is located verbatim in system/developer surfaces and replaced
    with a placeholder. When the exact text cannot be found (post-processing by
    the loader, truncation), the request is returned UNCHANGED and the caller
    must disclose 'persona present, not stripped' — never claim protection.
    Returns (request, replaced_count); mutates via copy-on-write.
    """
    needle = (persona_text or "").strip()
    if not needle or len(needle) < 32:
        return request, 0
    new_request = request
    replaced = 0

    def sub(text: str) -> str:
        nonlocal replaced
        if needle in text:
            replaced += text.count(needle)
            return text.replace(needle, _PERSONA_PLACEHOLDER)
        return text

    def ensure_copy() -> dict[str, Any]:
        nonlocal new_request
        if new_request is request:
            new_request = dict(request)
        return new_request

    instructions = request.get("instructions")
    if isinstance(instructions, str):
        text2 = sub(instructions)
        if text2 != instructions:
            ensure_copy()["instructions"] = text2
    messages = request.get("messages")
    if isinstance(messages, list):
        # Accumulate into ONE new list: rebuilding from the original snapshot
        # per replacement made a later replacement clobber an earlier one
        # (2026-11-11 adversarial round, found by test).
        new_messages: list[Any] | None = None

        def set_message(idx: int, msg2: dict[str, Any]) -> None:
            nonlocal new_messages, messages
            if new_messages is None:
                new_messages = list(messages)
            new_messages[idx] = msg2

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if msg.get("role") in ("system", "developer") and isinstance(content, str):
                text2 = sub(content)
                if text2 != content:
                    set_message(i, {**msg, "content": text2})
            if isinstance(content, list) and msg.get("role") in ("system", "developer"):
                # system/developer surfaces only: rewriting user/assistant/tool
                # text blocks silently altered quoted persona text and inflated
                # the strip count (round-#3 review).
                new_blocks: list[Any] | None = None
                for bi, part in enumerate(content):
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        text2 = sub(part["text"])
                        if text2 != part["text"]:
                            new_blocks = list(content) if new_blocks is None else new_blocks
                            new_blocks[bi] = {**part, "text": text2}
                if new_blocks is not None:
                    set_message(i, {**msg, "content": new_blocks})
        if new_messages is not None:
            ensure_copy()["messages"] = new_messages
    system = request.get("system")
    if isinstance(system, str):
        text2 = sub(system)
        if text2 != system:
            ensure_copy()["system"] = text2
    elif isinstance(system, list):
        new_system: list[Any] | None = None
        for i, part in enumerate(system):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text2 = sub(part["text"])
                if text2 != part["text"]:
                    new_system = list(system) if new_system is None else new_system
                    new_system[i] = {**part, "text": text2}
        if new_system is not None:
            ensure_copy()["system"] = new_system
    return new_request, replaced
