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
import threading
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
# Bounded history of past candidate sets (round-#4 Opus F2 availability fix):
# core renders the prompt snapshot at LOAD time, so a snapshot taken before a
# mid-session save matches the file version that existed then, not the current
# one. Every candidate is a real past/current file render (full-body exact
# match still required — provenance is not weakened, only widened to the
# versions the session actually observed).
_memory_bodies_history: dict[str, list] = {}
_MEMORY_HISTORY_LIMIT = 6
_history_lock = threading.Lock()

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
    bucket_key = "|".join(s[0] for s in sig)  # THIS profile's memory/profile paths
    bodies: list[str] = []
    # file label travels WITH its text (round-#5 Opus: when MEMORY.md is
    # unreadable, USER.md must not inherit MEMORY.md's placeholder name)
    labeled = [(("memory", "user")[i] if i < 2 else str(i), t) for i, t in enumerate(texts)]
    for idx, (file_label, text) in enumerate(labeled):
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
        # round-#4 Astra F5: core's LOADER replaces threat-matching entries
        # with a "[BLOCKED: ...]" placeholder in the frozen prompt snapshot
        # (load_from_disk -> _sanitize), so an unchanged file whose snapshot
        # was redacted can never match cleaned/raw. Add the redacted join as
        # a candidate, using core's own transform when importable.
        redacted = _core_redacted_join(entries, 0 if file_label == "memory" else 1, paths)
        if redacted and redacted not in bodies:
            bodies.append(redacted)
    _memory_bodies_cache = (key, bodies)
    with _history_lock:
        hist = _memory_bodies_history.setdefault(bucket_key, [])
        if not hist or hist[-1][0] != key:
            hist.append((key, bodies))
            del hist[:-_MEMORY_HISTORY_LIMIT]
    return bodies


def _memory_bodies_all(paths) -> list[str]:
    """Current candidates plus the bounded history of THIS profile's past
    versions, current first (round-#5 Astra F2: history buckets are keyed by
    the profile's memory/profile paths and are NEVER merged across profiles —
    another profile's observed body must not lend provenance here)."""
    current = _memory_bodies(paths)
    sig_paths = []
    for attr in ("memory_path", "user_profile_path"):
        p_ = getattr(paths, attr, None)
        if p_ is not None:
            sig_paths.append(str(p_))
    bucket_key = "|".join(sig_paths)
    out: list[str] = list(current)
    with _history_lock:
        for _sig, bodies in _memory_bodies_history.get(bucket_key, []):
            for b in bodies:
                if b not in out:
                    out.append(b)
    return out




def _core_redacted_join(entries: list[str], file_idx: int, paths) -> str | None:
    """Entries as core's loader renders them into the snapshot: threat hits
    replaced by the documented [BLOCKED: ...] placeholder, dedup preserved.
    Returns None when core's transform is unavailable (then only cleaned/raw
    candidates exist and redacted snapshots fail closed, as before)."""
    try:
        from tools.threat_patterns import scan_for_threats  # public allowlist
        filename = "MEMORY.md" if file_idx == 0 else "USER.md"
        target = "user" if filename == "USER.md" else "memory"

        def _sanitize(entry: str) -> str:
            if not entry or entry.startswith("[BLOCKED:"):
                return entry
            findings = scan_for_threats(entry, scope="strict")
            if not findings:
                return entry
            return (f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. "
                    f"Removed from system prompt; use memory(action=remove, target='{target}') "
                    f"to delete the original.]")

        return "\n§\n".join(dict.fromkeys(_sanitize(e) for e in entries))
    except Exception:
        return None


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


def _content_free_request(request: dict[str, Any]) -> dict[str, Any]:
    """An ALLOWLIST-scrubbed request for transports with no known response
    schema (round-#4 Astra F3): only non-content routing fields survive, so
    nothing can ride along — extra_body, metadata, extra_headers/query and
    every unlisted extension field are dropped (the OpenAI SDK merges
    extra_body into the serialized wire body even when top-level messages
    are empty). Residual disclosure: a downstream middleware could still
    reconstruct content from the chain's original_request context — that is
    documented, not silently claimed as protection."""
    return {"model": request.get("model")} if isinstance(request.get("model"), str) else {}


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
        # Append under the AUDIT lock (round-#4 Astra F7): a dedicated flock
        # so optional audit logging can never serialize a foreground request
        # behind a long deletion fence hold on the ledger lock. Concurrent
        # gateway/CLI processes still never interleave the cap rotation.
        from . import store as _store

        with _store.audit_lock(paths.data_dir):
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
    """The user's SOUL.md content exactly as core's loader normalizes it
    (round-#4 Opus F3: utf-8-sig so a leading BOM cannot hide the persona,
    stripped, legacy protocol header removed — the same passes core applies
    before injecting it into the system prompt)."""
    try:
        soul = paths.home / "SOUL.md"
        if not soul.exists():
            return None
        text = (soul.read_text(encoding="utf-8-sig") or "").strip()
        try:
            from tools.bot_mode_probe import strip_legacy_protocol  # public allowlist
            text = strip_legacy_protocol(text).strip()
        except Exception:
            pass
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
                new_request, bodies=_memory_bodies_all(paths))
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
    turn. api_modes with no synthetic shape get a CONTENT-FREE request (the
    turn fails downstream, but zero user content leaves the process).
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

            # Round-#4 Opus F2: MoA aggregation carries a prepared request
            # (core adds it AFTER llm_request) that neither the request-phase
            # strip nor the checks above inspect — and its advisor fan-out runs
            # through auxiliary calls before any middleware. Treat it as
            # unprovable content and block (disclosed in README What-stays).
            if block_message is None and request.get("_moa_prepared_request") is not None:
                block_message = _block_message(
                    verdict, "MoA aggregation is not enforced by private mode")

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
        # Unknown api_mode with no response shape: send a CONTENT-FREE request
        # through the chain (round-#3 close-out). The turn fails downstream
        # with a provider/transport error, but zero user content leaves the
        # process — fail-closed without knowing the response schema.
        logger.debug("ipm: llm_execution internal error -> content-free scrub")
        try:
            _audit_append(capture_paths(), {
                "provider": kwargs.get("provider"), "host": "", "scheme": "",
                "label": "internal-error-unshaped", "policy": "fail-closed-content-free",
                "blocked": True, "persona": None,
            })
        except Exception:
            pass
        return next_call(_content_free_request(request))
    if block_message is not None:
        # Shaping is guarded here too (round-4c): a construction RAISE must
        # not escape to core's fail-open middleware handler — fall back to
        # the dict shape, then to the content-free scrub, never to passthrough.
        try:
            shaped = synthetic.block_response(api_mode, block_message)
        except Exception as exc:
            logger.debug("ipm: synthetic shape raised (%s)", type(exc).__name__)
            shaped = None
        if shaped is not None:
            return shaped
        shaped = synthetic.block_response_dict(api_mode, block_message)
        if shaped is not None:
            return shaped
        # Unknown api_mode, positive block: send a CONTENT-FREE request — the
        # turn fails downstream with zero user content on the wire.
        logger.debug("ipm: block with no synthetic shape -> content-free scrub")
        return next_call(_content_free_request(request))
    return next_call(payload)


def _strip_ambiguous(payload: dict[str, Any]) -> bool:
    """True when a strip pass could not prove full removal (multi-paragraph
    entries under a fallback boundary, or unpaired context tags)."""
    if True:  # structure kept; exceptions now propagate (fail closed)
        def _text_residue(text: str) -> bool:
            if not isinstance(text, str):
                return False
            return (strip_mod.span_residue(text)
                    or strip_mod.has_memory_block(text)
                    or strip_mod.memory_title_residue(text))
        messages = payload.get("messages")
        if isinstance(messages, list):
            # Round-#4 Opus F3: user messages are checked only for the LAST
            # one (core injects memory spans per turn). A user who QUOTED a
            # memory block in an older turn must not wedge the session into a
            # permanent block.
            last_user_idx = max((i for i, m in enumerate(messages)
                                 if isinstance(m, dict) and strip_mod.is_user_message(m)), default=-1)
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                if strip_mod.is_system_message(msg, i):
                    for text in strip_mod.content_texts(msg.get("content")):
                        if strip_mod.has_memory_block(text) or strip_mod.memory_title_residue(text):
                            return True  # header survived (or lost its rules) — block
                if strip_mod.is_user_message(msg) and i == last_user_idx:
                    for text in strip_mod.content_texts(msg.get("content")):
                        if _text_residue(text):
                            return True
        inp = payload.get("input")
        if isinstance(inp, list):
            # same last-user-turn rule as messages (round-#5 Opus: a QUOTED
            # memory title in an older codex input turn must not wedge the
            # session; live memory in the current turn still blocks)
            user_idxs = [i for i, it in enumerate(inp)
                         if isinstance(it, dict) and it.get("role") == "user"]
            last_inp_user = user_idxs[-1] if user_idxs else -1
            for i, item in enumerate(inp):
                if isinstance(item, dict) and item.get("role") == "user" and i == last_inp_user:
                    for text in strip_mod.content_texts(item.get("content")):
                        if _text_residue(text):
                            return True
        system = payload.get("system")
        if system is not None:
            for text in strip_mod.content_texts(system):
                if strip_mod.memory_title_residue(text):
                    return True
        # codex_responses carries the system surface as `instructions`
        # (round-#4 Astra F2: the ASCII-sanitization residue check must cover
        # every transport's system surface, or mangled memory passes).
        instructions = payload.get("instructions")
        if instructions is not None:
            for text in strip_mod.content_texts(instructions):
                if strip_mod.memory_title_residue(text):
                    return True
        # Round-#4 Opus F11: no blanket except here — an error in the residue
        # scan must reach on_llm_execution's outer handler, which fails CLOSED.
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
