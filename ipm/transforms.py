"""Wire transforms: OpenRouter + Nous privacy flags (P0), copy-on-write
(BUILD_SPEC §3.7, REVIEW-astra M8). Never mutates the caller's request.

- OpenRouter/Nous: ``extra_body.provider`` += ``{"data_collection": "deny", "zdr": true}``
  (new dicts; keeps ``only``/``order``/``sort``; overrides an explicit ``allow`` with
  a trace reason).
- codex_responses: ``store`` is already ``False`` in core (agent/transports/codex.py);
  verify only, never set.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ipm.transforms")

FLAG_KEYS = {"data_collection": "deny", "zdr": True}


def apply_provider_routing(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (new_request, trace). Copy-on-write at every level touched."""
    trace: dict[str, Any] = {"applied": [], "overridden": [], "kept": []}
    new_request = dict(request)  # shallow copy; we replace only what we touch
    extra_body = new_request.get("extra_body")
    extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
    provider = extra_body.get("provider")
    provider = dict(provider) if isinstance(provider, dict) else {}
    # Preserve routing keys the user set (only/order/sort) — §3.7.
    for key in ("only", "order", "sort"):
        if key in provider:
            trace["kept"].append(key)
    if provider.get("data_collection") == "allow":
        trace["overridden"].append("data_collection=allow -> deny (private mode)")
    for key, value in FLAG_KEYS.items():
        if provider.get(key) != value:
            provider[key] = value
            trace["applied"].append(f"provider.{key}")
    extra_body["provider"] = provider
    new_request["extra_body"] = extra_body
    trace["ok"] = True
    return new_request, trace


def flags_present(request: dict[str, Any]) -> bool:
    provider = (request.get("extra_body") or {})
    if not isinstance(provider, dict):
        return False
    provider = provider.get("provider")
    if not isinstance(provider, dict):
        return False
    return provider.get("data_collection") == "deny" and provider.get("zdr") is True


def verify_store_false(request: dict[str, Any]) -> bool:
    """codex_responses requests must carry ``store: False`` (core sets it — verify only)."""
    return request.get("store") is False


def apply_for_route(request: dict[str, Any], flags: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dispatch on the route's flags kind. Unimplemented kinds pass through untouched."""
    kind = (flags or {}).get("kind")
    if kind == "provider_routing":
        return apply_provider_routing(request)
    trace = {"applied": [], "overridden": [], "kept": [], "ok": False, "kind": kind}
    return dict(request), trace


def memory_markers_present(request: dict[str, Any]) -> bool:
    """True if a recognised memory block or <memory-context> span survived into the
    final request while ``use_memory=false`` (block condition, §3.7)."""
    from . import strip as strip_mod

    texts = _collect_texts(request)
    for text in texts:
        if strip_mod.has_memory_block(text) or strip_mod.has_memory_context_span(text):
            return True
    return False


def _collect_texts(request: dict[str, Any]) -> list[str]:
    from . import strip as strip_mod

    out: list[str] = []
    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        out.extend(strip_mod.message_texts(messages))
    instructions = request.get("instructions")
    if isinstance(instructions, str):
        out.append(instructions)
    system = request.get("system")
    out.extend(strip_mod.anthropic_system_texts(system))
    inp = request.get("input")
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, dict):
                out.extend(strip_mod.content_texts(item.get("content")))
    return out
