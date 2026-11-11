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
import json
import os
from urllib.parse import urlsplit
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

_memory_bodies_cache: tuple = ((), [])

# Ambiguity signal from the llm_request strip pass, consumed by the same
# thread's llm_execution (request and execution hooks run sequentially on one
# thread for a given request; thread-local keeps concurrent requests isolated).
_strip_state = __import__("threading").local()


def _confirmations(data_dir) -> dict[str, Any]:
    from . import store

    return store.read("confirmations", data_dir)


def _memory_bodies(paths) -> list[str]:
    """Entry bodies of MEMORY.md / USER.md as core renders them (exact
    strip-reconstruction sources). Cached as ONE tuple so no reader can pair a
    new signature with stale bodies (round-#3 review: cache race).

    Core's file format is entries joined by '\n§\n' — no title line in the
    FILE (the title exists only in the rendered prompt block). Core reads with
    utf-8-sig (strips BOM) and dedups entries preserving order.
    """
    global _memory_bodies_cache
    sig = []
    texts = []
    for attr in ("memory_path", "user_profile_path"):
        p = getattr(paths, attr, None)
        if p is None:
            continue
        try:
            st = p.stat()
            sig.append((str(p), st.st_size, st.st_mtime_ns))
            texts.append(p.read_text(encoding="utf-8-sig", errors="replace"))
        except Exception:
            sig.append((str(p), None, None))
    key = tuple(sig)
    cached_sig, cached_bodies = _memory_bodies_cache
    if cached_sig == key:
        return cached_bodies
    bodies: list[str] = []
    for text in texts:
        segments = text.split("\n§\n")
        entries = [e.strip() for e in segments]
        entries = [e for e in entries if e]
        if not entries:
            continue
        # core-cleaned: dedup preserving order (dict.fromkeys, as core does)
        cleaned = "\n§\n".join(dict.fromkeys(entries))
        if cleaned not in bodies:
            bodies.append(cleaned)
        raw = "\n§\n".join(entries)
        if raw != cleaned and raw not in bodies:
            bodies.append(raw)
    _memory_bodies_cache = (key, bodies)
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
              confirmations: dict[str, Any], phase: str, paths: Any = None) -> routes_mod.RouteVerdict:
    provider = kwargs.get("provider")
    base_url = kwargs.get("base_url")
    api_mode = kwargs.get("api_mode")
    model = kwargs.get("model")
    auth_hint = _anthropic_auth_hint(request, api_mode)
    profile = routes_mod.profile_scope(getattr(paths, "home", None)) if paths is not None else ""
    verdict = routes_mod.classify(provider, base_url, api_mode, model, auth_hint,
                                  confirmations, None, profile=profile)
    return verdict


def _block_message(verdict: routes_mod.RouteVerdict, reason: str) -> str:
    route = ", ".join(verdict.row_ids) or "unknown route"
    return _BLOCK_TEMPLATE.format(route=route, reason=reason)


def _memory_strip_active(settings: dict[str, Any], entry: dict[str, Any] | None) -> bool:
    if effective_mode(settings, entry) == "off":
        return False
    return not settings.get("use_memory", False)


_AUDIT_MAX_BYTES = 512 * 1024


def _audit_append(paths: Any, record: dict[str, Any]) -> None:
    """Content-free outbound metadata (§2.5 discipline): endpoint/verdict/actions.
    JSONL at 0600; capped by keeping the newest half when it grows past 512KB."""
    try:
        path = paths.data_dir / "audit.jsonl"
        record = {"ts": round(time.time(), 3), **record}
        line = json.dumps(record, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except Exception:
            pass
        # Append under the store lock: concurrent gateway/CLI processes must
        # not interleave the truncate-and-rewrite cap rotation.
        from . import store as _store

        with _store.store_lock(paths.data_dir):
            if not path.exists():
                path.write_text(line + "\n")
                os.chmod(path, 0o600)
                return
            if path.stat().st_size > _AUDIT_MAX_BYTES:
                lines = path.read_text().splitlines()
                path.write_text("\n".join(lines[len(lines) // 2:]) + "\n")
            with open(path, "a") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # audit must never break the request path
        logger.debug("ipm: audit append failed (%s)", type(exc).__name__)


def _persona_in_payload(payload, persona: str) -> bool:
    """True when the persona text appears in any text surface of the request
    (instructions/system/messages/input). Never uses json.dumps: its escaping
    hides newlines/non-ASCII, which made multi-line personas report 'absent'
    while on the wire (round-#3 Astra F7)."""
    try:
        from .strip import content_texts
        needle = " ".join(persona[:120].split())
        surfaces: list[Any] = []
        if isinstance(payload, dict):
            surfaces.append(payload.get("instructions"))
            surfaces.append(payload.get("system"))
            messages = payload.get("messages")
            if isinstance(messages, list):
                surfaces.extend(m.get("content") for m in messages if isinstance(m, dict))
            inp = payload.get("input")
            if isinstance(inp, list):
                surfaces.extend(i.get("content") for i in inp if isinstance(i, dict))
        for surface in surfaces:
            for text in content_texts(surface):
                if needle in " ".join(text.split()):
                    return True
    except Exception:
        pass
    return False


def _persona_text(paths: Any) -> str | None:
    """The user's SOUL.md content as the loader normalizes it (stripped)."""
    try:
        soul = paths.home / "SOUL.md"
        if not soul.exists():
            return None
        text = (soul.read_text() or "").strip()
        return text or None
    except Exception:
        return None


def _plaintext_block_reason(kwargs: dict[str, Any], settings: dict[str, Any]) -> str | None:
    """Non-loopback http:// is cleartext egress: block unless explicitly allowed."""
    if settings.get("allow_plaintext_remote", False):
        return None
    base_url = kwargs.get("base_url")
    if not isinstance(base_url, str) or "://" not in base_url:
        return None
    parts = urlsplit(base_url)
    if parts.scheme != "http":
        return None
    host = (parts.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return None
    return "plaintext http to a remote host would leak content in the clear"


def on_llm_request(**kwargs: Any):
    """``llm_request`` middleware. Returns {"request": new} or None."""
    try:
        _strip_state.ambiguous = False  # no stale signal from a prior request
        _strip_state.persona_stripped = None  # stale flag could skip the strip
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        paths = capture_paths()
        settings = read_settings(paths)
        ledger, entry = _session_state(kwargs.get("session_id"), paths.data_dir)
        if not provider_controls_active(settings, entry):
            # §3.3: record that this chat observed an OFF period — a chat that
            # ever ran unprotected is never deleted later (round-#3 review:
            # arm -> off -> close -> on ended in a deletion).
            if entry is not None and kwargs.get("session_id"):
                from . import store as _store

                def _mark_off(l: dict, _sid: str = str(kwargs.get("session_id"))) -> dict:
                    e = l.get(_store.hash_session_id(_sid))
                    if isinstance(e, dict) and e.get("state") in {"armed", "candidate"}:
                        e["observed_off"] = True
                    return l
                try:
                    _store.mutate("ledger", paths.data_dir, _mark_off)
                except Exception:
                    pass
            return None
        # Lazy observation for sessions we never saw start: tombstone only (§3.3).
        session_id = kwargs.get("session_id")
        platform = kwargs.get("platform")
        if session_id and entry is None:
            _lazy_exclude(str(session_id), platform, paths.data_dir)

        new_request = request
        trace: dict[str, Any] = {"source": "incognito-private-mode", "applied": []}
        confirmations = _confirmations(paths.data_dir)
        verdict = _classify(new_request, kwargs, settings, confirmations, "request", paths)

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
            new_request, counts = strip_mod.strip_request(
                new_request, bodies=_memory_bodies(paths))
            _strip_state.ambiguous = bool(counts.get("ambiguous"))
            if counts["blocks"] or counts["spans"]:
                trace["applied"].append(
                    f"stripped memory blocks={counts['blocks']} spans={counts['spans']}")

        if settings.get("strip_persona", False):
            persona = _persona_text(paths)
            if persona:
                new_request, n = strip_mod.replace_persona_span(new_request, persona)
                _strip_state.persona_stripped = n
                if n:
                    trace["applied"].append(f"stripped persona spans={n}")
            else:
                _strip_state.persona_stripped = 0

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
            verdict = _classify(request, kwargs, settings, confirmations, "execution", paths)

            plaintext_reason = _plaintext_block_reason(kwargs, settings)
            if plaintext_reason:
                block_message = _block_message(verdict, plaintext_reason)
            elif verdict.label == "block" and settings.get("block_training", True):
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
                strip_ambiguous = bool(getattr(_strip_state, "ambiguous", False))
                _strip_state.ambiguous = False  # consume: one request, one signal
                if (transforms.memory_markers_present(payload) or _strip_ambiguous(payload)
                        or strip_ambiguous):
                    block_message = _block_message(
                        verdict, "memory content could not be proven fully removed "
                        "(exact reconstruction failed)")

            persona_state = None
            already = getattr(_strip_state, "persona_stripped", None)
            _strip_state.persona_stripped = None  # consume unconditionally
            if settings.get("strip_persona", False) and block_message is None:
                persona = _persona_text(paths)
                if persona and _persona_in_payload(payload, persona):
                    payload, n = strip_mod.replace_persona_span(payload, persona)
                    if n and not _persona_in_payload(payload, persona):
                        persona_state = "stripped"
                    else:
                        persona_state = "present-not-located"  # fail toward honesty
                elif persona and already:
                    # stripped in llm_request; verified absent from THIS payload
                    persona_state = "stripped"
                else:
                    persona_state = "absent"
            elif settings.get("strip_persona", False):
                persona_state = "absent"

            if settings.get("audit_log", True):
                from urllib.parse import urlsplit as _us
                _parts = _us(str(kwargs.get("base_url") or ""))
                _audit_append(paths, {
                    "provider": kwargs.get("provider"),
                    "host": _parts.hostname or "",
                    "scheme": _parts.scheme or "",
                    "label": verdict.label,
                    "policy": verdict.policy,
                    "blocked": block_message is not None,
                    "persona": persona_state,
                })
    except Exception:
        # Internal failure -> fail closed when this transport supports a
        # synthetic block; otherwise pass the llm_request copy through (the
        # upstream middleware already applied flags/strip on that copy).
        synthetic_msg = "Blocked by private mode: the private-mode check could not run."
        shaped = synthetic.block_response(api_mode, synthetic_msg)
        if shaped is None:
            # SDK construction failed: dict fallback for the three known modes
            # (round-#3 Astra F5 — a shaping failure must NEVER reach the
            # provider; internal errors are fail-closed, not fail-open).
            shaped = synthetic.block_response_dict(api_mode, synthetic_msg)
        if shaped is not None:
            logger.debug("ipm: llm_execution internal error -> synthetic block",
                         exc_info=True)
            try:
                _audit_append(capture_paths(), {
                    "provider": kwargs.get("provider"),
                    "host": "",
                    "scheme": "",
                    "label": "internal-error",
                    "policy": "fail-closed",
                    "blocked": True,
                    "persona": None,
                })
            except Exception:
                pass
            return shaped
        # Unknown api_mode with no shape at all: documented limitation —
        # core's middleware is fail-open for callback results, so the only
        # honest options are pass-through or a broken turn. Recorded in the
        # audit log; disclosed in the README.
        logger.debug("ipm: llm_execution re-check skipped (no synthetic shape)")
        try:
            _audit_append(capture_paths(), {
                "provider": kwargs.get("provider"), "host": "", "scheme": "",
                "label": "internal-error-unshaped", "policy": "fail-open-documented",
                "blocked": False, "persona": None,
            })
        except Exception:
            pass
        return next_call(request)
    if block_message is not None:
        return synthetic.block_response(api_mode, block_message)
    return next_call(payload)


def _strip_ambiguous(payload: dict[str, Any]) -> bool:
    """True when a strip pass could not prove full removal (multi-paragraph
    entries under a fallback boundary, or unpaired context tags)."""
    try:
        def _text_residue(text: str) -> bool:
            if not isinstance(text, str):
                return False
            return (strip_mod.span_residue(text)
                    or strip_mod.has_memory_block(text)
                    or strip_mod.memory_title_residue(text))
        messages = payload.get("messages")
        if isinstance(messages, list):
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                if strip_mod.is_system_message(msg, i):
                    for text in strip_mod.content_texts(msg.get("content")):
                        if strip_mod.has_memory_block(text) or strip_mod.memory_title_residue(text):
                            return True  # header survived (or lost its rules) — block
                if strip_mod.is_user_message(msg):
                    for text in strip_mod.content_texts(msg.get("content")):
                        if _text_residue(text):
                            return True
        inp = payload.get("input")
        if isinstance(inp, list):
            for item in inp:
                if isinstance(item, dict) and item.get("role") == "user":
                    for text in strip_mod.content_texts(item.get("content")):
                        if _text_residue(text):
                            return True
        system = payload.get("system")
        if system is not None:
            for text in strip_mod.content_texts(system):
                if strip_mod.memory_title_residue(text):
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
