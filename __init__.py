"""incognito-private-mode — "Private Mode, honestly" (BUILD_SPEC v3, AGPL-3.0).

register() only registers (plus attaching the log filter, §2.3). Every callback
catches its own exceptions -> safe outcome (block / veto / no deletion) and
logs content-free (§2.4/§2.5). Background threads carry the captured profile
paths and a copied context (§2.6).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ipm")

MANIFEST = {
    "name": "incognito-private-mode",
    "version": "0.3.0",
}


def register(ctx) -> None:
    from .ipm import commands, enforce, errors, lifecycle, logfilter

    ctx.register_middleware("llm_request", enforce.on_llm_request)
    ctx.register_middleware("llm_execution", enforce.on_llm_execution)
    ctx.register_hook("pre_tool_call", enforce.on_pre_tool_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_finalize", _on_finalize)
    ctx.register_hook("on_session_delete", _on_session_delete)
    ctx.register_hook("post_compaction", _on_post_compaction)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("pre_auxiliary_call", _on_pre_auxiliary_call)
    ctx.register_hook("transform_api_error_classification", _on_error_classification)
    ctx.register_command(
        "private", commands.handle_private_command,
        description="Private Mode: status, per-chat keep/on/off, cleanup, deep-clean",
        args_hint="[status|keep|on|off|cleanup|deep-clean]",
    )
    ctx.register_cli_command(
        "incognito-private-mode",
        help="Private Mode, honestly: status, cleanup, deep-clean, setup",
        setup_fn=commands.cli_register,
        handler_fn=commands.cli_handler,
    )
    # Process-wide log filter (disclosed in README + catalog PR), attached at the
    # earliest public point so the first message of a fresh process is covered.
    logfilter.attach()
    ctx.on_unload(logfilter.detach)


# --- hook adapters: capture profile paths at hook time (§2.6), fail safe (§2.4) ---

def _on_session_start(session_id: str | None = None, platform: str | None = None, **_kw: Any) -> None:
    try:
        from .ipm import lifecycle
        from .ipm.settings import capture_paths

        if session_id:
            lifecycle.observe_session_start(str(session_id), platform, capture_paths())
    except Exception:
        logger.debug("ipm: on_session_start skipped")


def _on_finalize(session_id: str | None = None, platform: str | None = None, **kwargs: Any) -> str | None:
    try:
        from .ipm import lifecycle
        from .ipm.settings import capture_paths

        return lifecycle.finalize(session_id, platform, capture_paths(), **kwargs)
    except Exception:
        logger.debug("ipm: on_session_finalize skipped")
        return None


def _on_session_delete(session_id: str | None = None, reason: str = "", **_kw: Any) -> None:
    try:
        from .ipm import lifecycle
        from .ipm.settings import capture_paths

        if session_id:
            lifecycle.on_external_delete(str(session_id), str(reason), capture_paths())
    except Exception:
        logger.debug("ipm: on_session_delete skipped")


def _on_post_compaction(session_id: str | None = None, old_session_id: str | None = None, **_kw: Any) -> None:
    try:
        from .ipm import lifecycle
        from .ipm.settings import capture_paths

        lifecycle.observe_post_compaction(session_id, old_session_id, capture_paths())
    except Exception:
        logger.debug("ipm: post_compaction skipped")


def _on_subagent_start(parent_session_id: str | None = None, child_session_id: str | None = None, **_kw: Any) -> None:
    try:
        from .ipm import lifecycle
        from .ipm.settings import capture_paths

        lifecycle.observe_subagent_start(parent_session_id, child_session_id, capture_paths())
    except Exception:
        logger.debug("ipm: subagent_start skipped")


def _on_pre_auxiliary_call(**kwargs: Any) -> None:
    """Observe only, fail-open (§3.1): content-free counts per provider for status."""
    try:
        from .ipm.settings import capture_paths, read_settings
        from .ipm import store

        provider = str(kwargs.get("provider") or "unknown")
        paths = capture_paths()
        settings = read_settings(paths)
        if settings.get("mode") == "off":
            return

        def fn(meta: dict) -> dict:
            counts = meta.get("aux_calls")
            counts = counts if isinstance(counts, dict) else {}
            counts[provider] = int(counts.get(provider, 0)) + 1
            meta["aux_calls"] = counts
            return meta

        store.mutate("meta", paths.data_dir, fn, blocking=False)
    except Exception:
        pass


def _on_error_classification(**kwargs: Any) -> dict[str, Any] | None:
    try:
        from .ipm import errors
        from .ipm.settings import capture_paths, read_settings

        settings = read_settings(capture_paths())
        provider = kwargs.get("provider")
        return errors.classify_error(kwargs, mode_active=settings.get("mode") != "off",
                                     provider=str(provider) if provider else None)
    except Exception:
        return None
