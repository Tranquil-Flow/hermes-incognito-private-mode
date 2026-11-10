"""llm_request and llm_execution middleware (BUILD_SPEC §3.1/§3.7).

llm_request: apply provider privacy flags (copy-on-write) + memory strip on the
outgoing copy; lazily observe unknown sessions (policy only -> excluded).
llm_execution: re-classify the FINAL request; re-apply flags as a second
chance; otherwise return a typed synthetic block (never raise — an internal
failure BLOCKS via the synthetic shape, or passes through only when no
synthetic shape exists for that api_mode). Never call next_call when blocked.
next_call is invoked exactly once, outside the guarded section: a downstream
provider error propagates as-is (never re-run the call, never swallow).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import routes as routes_mod
from . import strip as strip_mod
from . import synthetic
from . import transforms
from .settings import (
    capture_paths,
    cleanup_enrolled,
    effective_mode,
    platform_in_cleanup_scope,
    provider_controls_active,
    read_settings,
    route_detection_config,
)

logger = logging.getLogger("ipm.enforce")

_BLOCK_TEMPLATE = (
    "⛔ Private mode blocked this request to {route}: {reason}. "
    "Background calls (e.g. title) may already have run. "
    "Switch model or use /private off for this chat."
)

_memory_bodies_cache: dict[str, Any] = {"sig": None, "bodies": []}


def _confirmations(data_dir) -> dict[str, Any]:
    from . import store

    return store.read("confirmations", data_dir)


def _memory_bodies(paths) -> list[str]:
    """Entry bodies of MEMORY.md / USER.md as core renders them (exact
    strip-reconstruction sources). Cached on (path, size, mtime_ns)."""
    sig = []
    texts = []
    for attr in ("memory_path", "user_profile_path"):
        p = getattr(paths, attr, None)
        if p is None:
            continue
        try:
            st = p.stat()
            sig.append((str(p), st.st_size, st.st_mtime_ns))
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            sig.append((str(p), None, None))
    key = tuple(sig)
    if _memory_bodies_cache["sig"] == key:
        return _memory_bodies_cache["bodies"]
    bodies: list[str] = []
    for text in texts:
        entries = [e.strip() for e in text.split("\n§\n")]
        entries = [e for e in entries if e]
        if entries:
            bodies.append("\n§\n".join(entries))
    _memory_bodies_cache["sig"] = key
    _memory_bodies_cache["bodies"] = bodies
    return bodies


def _session_state(session_id: str | None, data_dir) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(ledger, entry) — read-only, lock-free snapshot; absent -> no entry."""
    from . import store

    if not session_id:
        return {}, None
    ledger = store.read("ledger", data_dir)
    return ledger, store.ledger_get(ledger, session_id)


def _lazy_exclude(session_id: str, platform: str | None, data_dir) -> None:
    """Ledger-less llm_request/pre_tool_call observations: policy only, tombstone
    excluded (§3.3) — unknown provenance is protected."""
    from . import store

    if not session_id or not store.validate_session_id(session_id):
        return

    def fn(ledger: dict[str, Any]) -> dict[str, Any]:
        key = store.hash_session_id(session_id)
        if key in ledger:
            return ledger
        ledger[key] = {
            "state": "excluded",
            "excluded_reason": "observed without provenance (policy only)",
            "platform": platform or "",
            "first_seen": time.time(),
        }
        return ledger

    store.mutate("ledger", data_dir, fn)


def _anthropic_auth_hint(request: dict[str, Any], api_mode: str | None) -> str | None:
    """Anthropic subscription marker (§3.7): OAuth requests start system with the
    Claude Code prefix (agent/anthropic_adapter.py)."""
    if api_mode != "anthropic_messages":
        return None
    system = request.get("system")
    texts = strip_mod.anthropic_system_texts(system)
    marker = "You are Claude Code, Anthropic's official CLI for Claude."
    if texts and str(texts[0]).startswith(marker):
        return "oauth"
    return "api_key"


def _classify(request: dict[str, Any], kwargs: dict[str, Any], settings: dict[str, Any],
              confirmations: dict[str, Any], phase: str) -> routes_mod.RouteVerdict:
    provider = kwargs.get("provider")
    base_url = kwargs.get("base_url")
    api_mode = kwargs.get("api_mode")
    model = kwargs.get("model")
    auth_hint = _anthropic_auth_hint(request, api_mode)
    verdict = routes_mod.classify(provider, base_url, api_mode, model, auth_hint,
                                  confirmations, None)
    return verdict


def _block_message(verdict: routes_mod.RouteVerdict, reason: str) -> str:
    route = ", ".join(verdict.row_ids) or "unknown route"
    return _BLOCK_TEMPLATE.format(route=route, reason=reason)


def _memory_strip_active(settings: dict[str, Any], entry: dict[str, Any] | None) -> bool:
    if effective_mode(settings, entry) == "off":
        return False
    return not settings.get("use_memory", False)


def on_llm_request(**kwargs: Any):
    """``llm_request`` middleware. Returns {"request": new} or None."""
    try:
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        paths = capture_paths()
        settings = read_settings(paths)
        ledger, entry = _session_state(kwargs.get("session_id"), paths.data_dir)
        if not provider_controls_active(settings, entry):
            return None
        # Lazy observation for sessions we never saw start: tombstone only (§3.3).
        session_id = kwargs.get("session_id")
        platform = kwargs.get("platform")
        if session_id and entry is None:
            _lazy_exclude(str(session_id), platform, paths.data_dir)

        new_request = request
        trace: dict[str, Any] = {"source": "incognito-private-mode", "applied": []}
        confirmations = _confirmations(paths.data_dir)
        verdict = _classify(new_request, kwargs, settings, confirmations, "request")

        # 1. Provider flags (PER_REQUEST routes we implement).
        if verdict.flags.get("kind") == "provider_routing" and not transforms.flags_present(new_request):
            new_request, tr = transforms.apply_provider_routing(new_request)
            trace["applied"].extend(tr["applied"])
            if tr["overridden"]:
                trace["reason"] = "; ".join(tr["overridden"])

        # 2. Memory strip (§3.5) — wire-level only. Exact-reconstruction sources
        # are fed from the captured memory files so multi-paragraph entries are
        # removed whole (blank lines inside entries are legal in core).
        if _memory_strip_active(settings, entry):
            strip_mod.set_memory_bodies(_memory_bodies(paths))
            new_request, counts = strip_mod.strip_request(new_request)
            strip_mod.reset_memory_bodies()
            if counts["blocks"] or counts["spans"]:
                trace["applied"].append(
                    f"stripped memory blocks={counts['blocks']} spans={counts['spans']}")

        if new_request is not request:
            return {"request": new_request, "source": trace["source"],
                    "reason": trace.get("reason") or "private mode flags/strip"}
        return None
    except Exception as exc:
        logger.debug("ipm: llm_request middleware skipped (%s)", type(exc).__name__)
        return None  # fail open here; llm_execution re-checks the final request


def on_llm_execution(**kwargs: Any):
    """``llm_execution`` middleware. Re-checks the final request (§3.7 block rules):
    - ⛔ route and block_training -> block
    - ⚠️ route and not allow_unverified -> block
    - required flag missing after second chance -> block
    - memory block present (or strip ambiguous) with use_memory=false -> block

    On an INTERNAL error we fail CLOSED where the transport allows a synthetic
    block: an unchecked request to a training route is worse than a failed
    turn. Pass-through remains only for api_modes with no synthetic shape.
    next_call is invoked exactly once, OUTSIDE the guarded section: a downstream
    provider error must propagate as-is (never re-run the call, never swallow).
    """
    next_call = kwargs.get("next_call")
    request = kwargs.get("request")
    if not isinstance(request, dict) or not callable(next_call):
        return next_call(request) if callable(next_call) else None
    api_mode = kwargs.get("api_mode")
    block_message: str | None = None
    payload = request
    try:
        paths = capture_paths()
        settings = read_settings(paths)
        ledger, entry = _session_state(kwargs.get("session_id"), paths.data_dir)
        if not provider_controls_active(settings, entry):
            payload = request  # passthrough
        else:
            confirmations = _confirmations(paths.data_dir)
            verdict = _classify(request, kwargs, settings, confirmations, "execution")

            if verdict.label == "block" and settings.get("block_training", True):
                block_message = _block_message(verdict, "; ".join(verdict.reasons) or "route trains with no opt-out")
            elif verdict.label in {"warn", "needs-confirmation"} and not settings.get("allow_unverified", True):
                block_message = _block_message(verdict, "; ".join(verdict.reasons) or "route not verified for privacy")
            elif verdict.label == "ok-if-flagged":
                required = verdict.flags.get("kind")
                if required == "provider_routing" and not transforms.flags_present(payload):
                    payload, _tr = transforms.apply_provider_routing(payload)  # second chance
                    if not transforms.flags_present(payload):
                        block_message = _block_message(verdict, "required privacy flag missing")
                elif required == "verify_store_false" and not transforms.verify_store_false(payload):
                    block_message = _block_message(verdict, "Responses request missing store:false")
            elif verdict.label in {"ok", "confirmed"}:
                if verdict.flags.get("kind") == "provider_routing" and not transforms.flags_present(payload):
                    payload, _tr = transforms.apply_provider_routing(payload)

            if block_message is None and _memory_strip_active(settings, entry):
                if transforms.memory_markers_present(payload) or _strip_ambiguous(payload):
                    block_message = _block_message(verdict, "memory content still present in request")
    except Exception:
        # Internal failure -> fail closed when this transport supports a
        # synthetic block; otherwise pass the llm_request copy through (the
        # upstream middleware already applied flags/strip on that copy).
        synthetic_msg = "Blocked by private mode: the private-mode check could not run."
        shaped = synthetic.block_response(api_mode, synthetic_msg)
        if shaped is not None:
            logger.debug("ipm: llm_execution internal error -> synthetic block")
            return shaped
        logger.debug("ipm: llm_execution re-check skipped (no synthetic shape)")
        return next_call(request)
    if block_message is not None:
        return synthetic.block_response(api_mode, block_message)
    return next_call(payload)


def _strip_ambiguous(payload: dict[str, Any]) -> bool:
    """True when a strip pass could not prove full removal (multi-paragraph
    entries under a fallback boundary, or unpaired context tags)."""
    try:
        messages = payload.get("messages")
        if isinstance(messages, list):
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                if strip_mod.is_system_message(msg, i) and strip_mod.has_memory_block(
                        str(msg.get("content")) if isinstance(msg.get("content"), str) else ""):
                    return True  # header survived a strip pass
                if strip_mod.is_user_message(msg) and isinstance(msg.get("content"), str):
                    if strip_mod.span_residue(msg["content"]):
                        return True
        inp = payload.get("input")
        if isinstance(inp, list):
            for item in inp:
                if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), str):
                    if strip_mod.span_residue(item["content"]):
                        return True
    except Exception:
        return False
    return False


def on_pre_tool_call(**kwargs: Any):
    """pre_tool_call hook returning the block directive, or None to allow.
    Fail closed inside our code (§2.4): an internal error vetoes the call with a
    content-free message rather than letting it through unchecked."""
    from . import vetoes

    try:
        tool_name = kwargs.get("tool_name")
        paths = capture_paths()
        settings = read_settings(paths)
        ledger, entry = _session_state(kwargs.get("session_id"), paths.data_dir)
        memory_provider = route_detection_config(paths).get("memory_provider")
        message = vetoes.veto_reason(tool_name, settings, entry, memory_provider)
        if message:
            return {"action": "block", "message": message}
        return None
    except Exception:
        return {"action": "block",
                "message": "Blocked by private mode: the private-mode check could not run."}
