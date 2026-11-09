"""Process-wide log filter (BUILD_SPEC §3.6, disclosed).

Attached in register() (the earliest public point) to the emitting loggers AND
their handlers for:
- ``gateway.run``  — inbound message line (gateway/run_turn.py, ``msg=%r`` arg idx 3, reply idx 5)
- ``agent.turn_context`` — conversation turn line (msg arg idx 5)
- ``hermes.tool_calls`` — tool-call lines (its own handler writes logs/tool_calls.log)

Redacts msg/args for ALL records of those formats when mode != off (records
carry no reliable session id). Records written before the plugin loaded cannot
be redacted (disclosed). Content-free logging on failure (§2.5).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

TARGET_LOGGERS = ("gateway.run", "agent.turn_context", "hermes.tool_calls")
_MARKER = "_ipm_private_filter"

# Rendered f-string / %r fallback: msg='...' possibly with escapes.
_MSG_RENDERED_RE = re.compile(r"(\bmsg=)(['\"])(?:\\.|(?!\2).)*\2")
_REPLY_TEXT_RE = re.compile(r"(\breply_to_text=)(['\"])(?:\\.|(?!\2).)*\2")


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
        new = _MSG_RENDERED_RE.sub(r"\1\2[redacted]\2", rendered)
        new = _REPLY_TEXT_RE.sub(r"\1\2[redacted]\2", new)
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
    """Attach the filter to the target loggers and their handlers. Idempotent."""
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
        for handler in list(lg.handlers) + list(logging.getLogger().handlers):
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
        for handler in list(lg.handlers) + list(logging.getLogger().handlers):
            for f in list(handler.filters):
                if getattr(f, _MARKER, False):
                    handler.removeFilter(f)
                    removed += 1
    return removed


def stats() -> dict[str, Any]:
    return {"redacted_records": _shared_filter.redacted if _shared_filter else 0}
