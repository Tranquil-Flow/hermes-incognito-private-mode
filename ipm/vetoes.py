"""pre_tool_call veto table (BUILD_SPEC §3.4).

Block (default names): memory (save_memory), skill_manage (learn_skills),
session_search (keep_history), plus an external memory-provider's retain tool
when the provider registers a tool literally named after the provider id
(discovery limit disclosed in README/BUILD_LOG — other retain-tool names are
NOT covered in v1).

Message: ``Blocked by private mode: <thing> is off for this chat.``
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ipm.vetoes")

VETO_TABLE = {
    "memory": ("save_memory", "memory saving"),
    "skill_manage": ("learn_skills", "skill learning"),
    "session_search": ("keep_history", "past-chat search"),
}


def veto_reason(tool_name: str | None, settings: dict[str, Any],
                entry: dict[str, Any] | None,
                memory_provider: str | None) -> str | None:
    """Return the block message when this tool call must be vetoed, else None.

    *entry* is the ledger entry for the calling session (per-chat overrides);
    vetoes apply whenever the chat's effective mode is private, on all platforms.
    Background-review forks carry the parent session id, so they are covered too.
    """
    if not tool_name:
        return None
    override = (entry or {}).get("override")
    if override == "off":
        return None
    settings = settings or {}
    if settings.get("mode") == "off" and override not in {"on", "keep"}:
        return None
    if tool_name in VETO_TABLE:
        toggle, thing = VETO_TABLE[tool_name]
        if not settings.get(toggle, False):
            return f"Blocked by private mode: {thing} is off for this chat."
    if memory_provider and tool_name == memory_provider and not settings.get("save_memory", False):
        return f"Blocked by private mode: memory saving is off for this chat."
    return None
