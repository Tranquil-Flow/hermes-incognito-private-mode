"""Settings + path capture for incognito-private-mode.

Every hook captures the Hermes home, the state DB path and the plugin data dir
*at hook time* (BUILD_SPEC §2.6) and passes them as immutable inputs to any
background work. Nothing here re-derives a destructive target inside a thread.

Public imports only (hermes_cli/plugin_validate_private_imports.py):
``hermes_constants.get_hermes_home``, ``hermes_cli.config.load_config_readonly``,
``hermes_cli.plugins_state.PluginState``.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("ipm.settings")

PLUGIN_ID = "incognito-private-mode"

DEFAULTS: dict[str, Any] = {
    "mode": "private",
    # §6: "No default deletion on a surface whose row is not pass." Rows 8/10/15
    # (TUI/Desktop interactive surfaces) are not independently e2e-proven, so
    # cleanup defaults to cli only; tui/desktop are opt-in via this key.
    "apply_to_platforms": ["cli"],
    "remove_history_on_close": True,
    "keep_history": False,
    "use_memory": False,
    "save_memory": False,
    "learn_skills": False,
    "allow_unverified": True,
    # Block http:// requests to non-loopback hosts (cleartext egress).
    "allow_plaintext_remote": False,
    # Best-effort removal of the user's SOUL.md persona from the system prompt
    # (honest: when the exact text cannot be located, status says so).
    "strip_persona": False,
    # Content-free outbound metadata log (endpoint/verdict/actions), local 0600.
    "audit_log": True,
    "block_training": True,
    "deep_clean_reminder": "weekly",
}

VALID_MODES = {"private", "off"}
VALID_PLATFORMS = {"cli", "tui", "desktop"}
LOCAL_PLATFORMS = {"cli", "tui", "desktop"}
CLEANUP_PLATFORM_SCOPE = VALID_PLATFORMS  # messaging cleanup is post-hackathon (§3.10)

_config_cache: dict[str, Any] = {"mtime": None, "settings": None}
_cache_lock = threading.Lock()


class CapturedPaths:
    """Immutable profile inputs captured inside the invoking hook."""

    __slots__ = ("home", "db_path", "data_dir", "sessions_dir", "logs_dir",
                 "memory_path", "user_profile_path")

    def __init__(self, home: Path) -> None:
        self.home = home
        self.db_path = home / "state.db"
        self.sessions_dir = home / "sessions"
        self.logs_dir = home / "logs"
        # Memory strip sources (§3.5): the files core renders MEMORY/USER
        # blocks from. Their EXISTENCE is not assumed — readers handle
        # OSError/None.
        self.memory_path = home / "memories" / "MEMORY.md"
        self.user_profile_path = home / "memories" / "USER.md"
        try:
            from hermes_cli.plugins_state import PluginState  # public allowlist

            self.data_dir = PluginState(PLUGIN_ID).data_dir
        except Exception:  # pragma: no cover - PluginState is stable; fail closed to a home-scoped dir
            self.data_dir = home / "plugin-data" / PLUGIN_ID

    def as_tuple(self) -> tuple[Path, Path, Path]:
        return (self.home, self.db_path, self.data_dir)


def capture_paths() -> CapturedPaths:
    """Capture the active profile's paths in the caller's context (§2.6)."""
    from hermes_constants import get_hermes_home  # public allowlist

    return CapturedPaths(get_hermes_home())


def _settings_entry(config: dict[str, Any]) -> dict[str, Any]:
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return {}
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return {}
    settings = entry.get("settings")
    return settings if isinstance(settings, dict) else {}


def read_settings(paths: CapturedPaths | None = None) -> dict[str, Any]:
    """Effective settings: config_schema defaults overridden by
    ``plugins.entries.incognito-private-mode.settings``. Live re-read with an
    mtime cache so the gateway picks up edits without a restart."""
    paths = paths or capture_paths()
    config_path = paths.home / "config.yaml"
    try:
        st = config_path.stat()
        cache_key = (str(config_path), st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        cache_key = (str(config_path), None, None)
    with _cache_lock:
        cached_key = _config_cache["mtime"]
        if cache_key[1] is not None and cached_key == cache_key and _config_cache["settings"] is not None:
            return dict(_config_cache["settings"])
    settings = dict(DEFAULTS)
    try:
        from hermes_cli.config import load_config_readonly  # public allowlist

        config = load_config_readonly() or {}
        settings.update(_settings_entry(config))
    except Exception as exc:  # fail closed to defaults, log content-free
        logger.debug("ipm: settings read failed (%s)", type(exc).__name__)
        # An unreadable config must not silently WIDEN deletion: defaults carry
        # remove_history_on_close=True, which would drop a user's explicit
        # keep_history:true / narrowed surfaces (round-#3 lifecycle worker).
        settings["remove_history_on_close"] = False
        settings["apply_to_platforms"] = []
    settings = _validated(settings)
    with _cache_lock:
        _config_cache["mtime"] = cache_key
        _config_cache["settings"] = dict(settings)
    return dict(settings)


def _validated(settings: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    # `mode: off` unquoted parses as boolean False — that is the user's typed
    # intent, honor it before the generic str coercion (which would silently
    # turn it into 'private', a protection change).
    raw_mode = settings.get("mode")
    if raw_mode is False:
        settings = dict(settings)
        settings["mode"] = "off"
    # Deletion-scope keys fail toward NO deletion before the generic coercion
    # (whose default for remove_history_on_close is True — a widening default).
    settings = dict(settings)
    if not isinstance(settings.get("remove_history_on_close"), bool):
        settings["remove_history_on_close"] = False
    if not isinstance(settings.get("apply_to_platforms"), list):
        settings["apply_to_platforms"] = []
    for key, default in DEFAULTS.items():
        value = settings.get(key, default)
        if isinstance(default, bool):
            out[key] = bool(value) if isinstance(value, bool) else default
        elif isinstance(default, list):
            out[key] = [v for v in value if isinstance(v, str)] if isinstance(value, list) else list(default)
        elif isinstance(default, str):
            out[key] = str(value) if isinstance(value, str) else default
    if out["mode"] is True or out["mode"] not in VALID_MODES:
        out["mode"] = DEFAULTS["mode"]  # unknown -> default (private)
    if out["deep_clean_reminder"] not in {"off", "weekly", "monthly"}:
        out["deep_clean_reminder"] = "weekly"
    out["apply_to_platforms"] = [p for p in out["apply_to_platforms"] if p in VALID_PLATFORMS]
    if not out["apply_to_platforms"]:
        # Deletion scope fails toward NO deletion: an invalid value (e.g. 'TUI'
        # typed in the wizard, 'none', or a non-list) must not silently widen
        # cleanup to the default platforms (2026-11-11 adversarial round).
        out["apply_to_platforms"] = []
    # Non-bool remove_history_on_close must not coerce to deletion.
    if not isinstance(out["remove_history_on_close"], bool):
        out["remove_history_on_close"] = False
    return out


def cleanup_enrolled(settings: dict[str, Any], meta: dict[str, Any]) -> bool:
    """True only when history removal is enabled AND setup consent is recorded (§3.9)."""
    if not settings.get("remove_history_on_close"):
        return False
    consent = meta.get("consent") if isinstance(meta, dict) else None
    return bool(isinstance(consent, dict) and consent.get("recorded") is True)


def platform_in_cleanup_scope(platform: str | None, settings: dict[str, Any]) -> bool:
    if platform is None:
        return False
    if platform not in CLEANUP_PLATFORM_SCOPE:
        return False
    return platform in set(settings.get("apply_to_platforms") or [])


def provider_controls_active(settings: dict[str, Any], entry: dict[str, Any] | None) -> bool:
    """Provider flags, blocks, strip and vetoes apply on ALL platforms while the
    chat's effective mode is not off (they delete nothing) — §3.2.

    Audit fix: this used to read the GLOBAL mode only, so `/private on` in a
    chat while mode=off widened the vetoes but silently enabled no flags, no
    strip and no blocks — contradicting effective_mode's documented behaviour.
    It now defers to the per-chat effective mode."""
    return effective_mode(settings, entry) != "off"


def effective_mode(settings: dict[str, Any], entry: dict[str, Any] | None) -> str:
    """Per-chat effective mode. ``/private on`` forces private even when mode=off;
    ``/private off`` disables everything for the chat; ``keep`` behaves like on for
    policy but excludes the lineage from cleanup (§3.2/§3.3)."""
    override = (entry or {}).get("override")
    if override == "off":
        return "off"
    if settings.get("mode") == "off":
        return "private" if override in {"on", "keep"} else "off"
    return "private"


def route_detection_config(paths: CapturedPaths | None = None) -> dict[str, Any]:
    """Config facts used for coverage lines and setup: main model route,
    fallback providers, explicit aux routes, runtime and memory provider."""
    paths = paths or capture_paths()
    facts: dict[str, Any] = {
        "main": None,
        "fallbacks": [],
        "aux": [],
        "runtime": None,
        "memory_provider": None,
        "config_error": None,
    }
    try:
        from hermes_cli.config import load_config_readonly  # public allowlist

        config = load_config_readonly() or {}
    except Exception as exc:
        facts["config_error"] = type(exc).__name__
        return facts
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    if model:
        facts["main"] = {
            "provider": model.get("provider"),
            "base_url": model.get("base_url"),
            "model": model.get("default") or model.get("model"),
            "api_mode": model.get("api_mode"),
        }
        facts["runtime"] = model.get("openai_runtime") or None
    fallbacks = config.get("fallback_providers")
    if isinstance(fallbacks, list):
        for fb in fallbacks:
            if isinstance(fb, dict):
                facts["fallbacks"].append({
                    "provider": fb.get("provider"),
                    "base_url": fb.get("base_url"),
                    "model": fb.get("model") or fb.get("default"),
                    "api_mode": fb.get("api_mode"),
                })
    aux = config.get("auxiliary")
    if isinstance(aux, dict):
        for task, block in aux.items():
            if not isinstance(block, dict):
                continue
            provider = block.get("provider", "auto")
            base_url = block.get("base_url") or ""
            # "auto" = inherit the main model: not an explicit route (§3.7).
            if provider in (None, "", "auto") and not base_url:
                continue
            facts["aux"].append({
                "task": str(task),
                "provider": provider,
                "base_url": base_url,
                "model": block.get("model") or "",
            })
    memory = config.get("memory")
    if isinstance(memory, dict):
        provider = memory.get("provider")
        # Mirror core's core-provider sentinels (audit fix): 'default' and
        # 'built-in' are also built-in markers, not external providers.
        sentinels = {None, "", "builtin", "built-in", "none", "default"}
        if provider not in sentinels:
            facts["memory_provider"] = str(provider)
    return facts
