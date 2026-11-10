"""Process-wide log filter (BUILD_SPEC §3.6, disclosed).

Attached in register() (the earliest public point) to the emitting loggers AND
their handlers for:
- ``gateway.run``  — inbound message line (gateway/run_turn.py, ``msg=%r`` arg idx 3, reply idx 5)
- ``agent.turn_context`` — conversation turn line (msg arg idx 5)
- ``hermes.tool_calls`` — tool-call lines (``<ts>  <tool>: "<preview>"``; the
  preview carries tool-argument content and is redacted whole)

Redacts msg/args for ALL records of those formats when mode != off (records
carry no reliable session id). Records written before the plugin loaded cannot
be redacted (disclosed). Content-free logging on failure (§2.5).

ReDoS safety (post-audit): the rendered-text regexes use UNAMBIGUOUS quoted-
string alternation (``(?:[^'\\n\\"]|\\.)*`` — no backslash double-consumption),
and rendered messages are capped at _SCAN_CAP chars. No exponential
backtracking is possible.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

TARGET_LOGGERS = ("gateway.run", "agent.turn_context", "hermes.tool_calls")
_MARKER = "_ipm_private_filter"
_SCAN_CAP = 4096  # rendered chars scanned for redaction; beyond this, pass as-is

# Unambiguous quoted-string forms: quote char is excluded from the bare-char
# alternation, so a backslash cannot be consumed two ways (no catastrophic
# backtracking). Each of these is linear in the scanned region.
_MSG_RENDERED_RE = re.compile(r"\bmsg=(?:'[^'\\\n]*'|\"[^\"\\\n]*\"|'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")")
_REPLY_TEXT_RE = re.compile(r"\breply_to_text=(?:'[^'\\\n]*'|\"[^\"\\\n]*\"|'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")")
_MSG_UNQUOTED_RE = re.compile(r"(\bmsg=)([^\s]+(?:\s+(?!\w+=)[^\s=]+)*)")
# hermes.tool_calls lines: '<ts>  <tool_name>: "<preview>"' — redact the preview.
_TOOL_PREVIEW_RE = re.compile('^(\\S+\\s+\\S+\\s+\\S+:\\s)(["\'])(?:[^"\'\\\\n]|\\\\.)*\\2')


class PrivateLogFilter(logging.Filter):
    """Redact message content on known content-bearing formats."""

    def __init__(self, mode_active: Callable[[], bool]) -> None:
        super().__init__()
        self._mode_active = mode_active
        self.redacted = 0  # counts only; never content

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if not self._mode_active():
                return True
            self._redact(record)
        except Exception:
            pass  # fail open: the record passes through unmodified
        return True  # we redact in place; never drop records

    def _redact(self, record: logging.LogRecord) -> None:
        args = record.args
        msg = record.msg
        if isinstance(msg, str) and args:
            if isinstance(args, tuple) and msg.startswith("inbound message: platform=") and len(args) >= 4:
                args = list(args)
                args[3] = "[redacted]"
                if len(args) >= 6:
                    args[5] = "[redacted]"
                record.args = tuple(args)
                self.redacted += 1
                return
            if isinstance(args, tuple) and msg.startswith("conversation turn: session=") and len(args) >= 6:
                args = list(args)
                args[5] = "[redacted]"
                record.args = tuple(args)
                self.redacted += 1
                return
        # Formatted (f-string) or unknown-args case: redact the rendered message.
        try:
            rendered = record.getMessage()
        except Exception:
            return
        if record.name == "hermes.tool_calls":
            new = _TOOL_PREVIEW_RE.sub(r"\1[redacted]", rendered)
        else:
            scan = rendered[:_SCAN_CAP]
            new = _MSG_RENDERED_RE.sub(lambda m: m.group(0)[: m.group(0).index("=") + 1] + "[redacted]", scan)
            if new == scan:
                new = _MSG_UNQUOTED_RE.sub(r"\1[redacted]", scan)
            new = _REPLY_TEXT_RE.sub(lambda m: m.group(0)[: m.group(0).index("=") + 1] + "[redacted]", new)
            if len(rendered) > _SCAN_CAP:
                new = new + rendered[_SCAN_CAP:]
        if new != rendered:
            record.msg = new
            record.args = None
            self.redacted += 1


def _mode_active_from_settings() -> bool:
    try:
        from .settings import read_settings

        return read_settings().get("mode") != "off"
    except Exception:
        return False


_shared_filter: PrivateLogFilter | None = None


def attach(mode_active: Callable[[], bool] | None = None) -> int:
    """Attach the filter to the target loggers and their handlers. Idempotent.

    Root handlers are NOT touched (post-audit): the filter would otherwise run
    on every record in the process. Records reaching the target loggers through
    propagate/root handlers are covered by the logger-level filter; records on
    unrelated loggers are none of this filter's business (§3.6 scope).
    """
    global _shared_filter
    if mode_active is None:
        mode_active = _mode_active_from_settings
    if _shared_filter is None:
        _shared_filter = PrivateLogFilter(mode_active)
    attached = 0
    for name in TARGET_LOGGERS:
        lg = logging.getLogger(name)
        setattr(_shared_filter, _MARKER, True)
        if not any(getattr(f, _MARKER, False) for f in lg.filters):
            lg.addFilter(_shared_filter)
            attached += 1
        for handler in list(lg.handlers):
            if not any(getattr(f, _MARKER, False) for f in handler.filters):
                handler.addFilter(_shared_filter)
                attached += 1
    return attached


def detach() -> int:
    """Remove our filters (ctx.on_unload)."""
    removed = 0
    for name in TARGET_LOGGERS:
        lg = logging.getLogger(name)
        for f in list(lg.filters):
            if getattr(f, _MARKER, False):
                lg.removeFilter(f)
                removed += 1
        for handler in list(lg.handlers):
            for f in list(handler.filters):
                if getattr(f, _MARKER, False):
                    handler.removeFilter(f)
                    removed += 1
    return removed


def stats() -> dict[str, Any]:
    return {"redacted_records": _shared_filter.redacted if _shared_filter else 0}
