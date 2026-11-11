"""Route classification, worst-verdict, and the three-line coverage model
(BUILD_SPEC §3.7). Pure functions over routes.json + confirmations.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

from .settings import DEFAULTS as _SETTINGS_DEFAULTS

logger = logging.getLogger("ipm.routes")

VERDICT_ORDER = [
    "ALREADY", "PER_REQUEST", "ACCOUNT_SETTING", "ORG_AGREEMENT",
    "UNVERIFIED", "NONE_RETENTION", "NONE_TRAINS",
]
LABELS = {
    "ALREADY": "ok",
    "PER_REQUEST": "ok-if-flagged",
    "ACCOUNT_SETTING": "needs-confirmation",
    "ORG_AGREEMENT": "needs-confirmation",
    "UNVERIFIED": "warn",
    "NONE_RETENTION": "warn",
    "NONE_TRAINS": "block",
    "UNKNOWN": "warn",
}
# Rows a user confirmation may promote (§3.7) — never PER_REQUEST/NONE_*/ALREADY.
CONFIRMABLE = frozenset({"ACCOUNT_SETTING", "ORG_AGREEMENT"})
POSITIVE_LABELS = frozenset({"ok", "ok-if-flagged", "confirmed"})
# Severity for picking the worst of several verdicts (main + fallbacks, aux):
# by LABEL first (what enforcement does), then by policy.
_LABEL_RANK = {"ok": 0, "confirmed": 1, "ok-if-flagged": 2, "needs-confirmation": 3, "warn": 4, "block": 5}

_route_cache: dict[str, Any] = {}


def load_table(data_dir: Path | None = None) -> dict[str, Any]:
    """Parse ipm/routes.json (shipped with the plugin), mtime-cached."""
    table_path = Path(__file__).resolve().parent / "routes.json"
    try:
        mtime = table_path.stat().st_mtime_ns
    except OSError:
        mtime = None
    cached = _route_cache.get("mtime")
    if cached == mtime and _route_cache.get("table") is not None:
        return _route_cache["table"]
    try:
        table = json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("ipm: routes.json unreadable (%s)", type(exc).__name__)
        table = {"policy_rev": "error", "rows": []}
    rows = [r for r in table.get("rows", []) if isinstance(r, dict)]
    table["rows"] = rows
    _route_cache["mtime"] = mtime
    _route_cache["table"] = table
    return table


def host_of(base_url: str | None) -> str:
    if not base_url:
        return ""
    try:
        host = (urlparse(str(base_url)).hostname or "").lower()
    except ValueError:
        return ""
    return host


def is_loopback(base_url: str | None) -> bool:
    return _loopback_host(host_of(base_url))


def _loopback_host(host: str) -> bool:
    """Bare-host variant (host_of output); urlparse has no scheme for bare
    names, so a host string must be checked directly."""
    return (host or "").lower() in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


@dataclass
class RouteVerdict:
    label: str                      # ok | ok-if-flagged | needs-confirmation | warn | block | confirmed
    policy: str                     # worst policy, or UNKNOWN
    row_ids: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    account_setting: str = ""
    reasons: list[str] = field(default_factory=list)
    policy_rev: str = ""
    confirmed: bool = False
    # Source links of every matched row (routes.json ``sources[].url``).
    sources: list[str] = field(default_factory=list)
    host: str = ""
    # Why a better label was capped: uncurated-host | host-mismatch | loopback |
    # runtime-endpoint (status prediction) | auto-routing (no aux route) | "".
    cap_reason: str = ""
    # Which configured route this verdict describes (classify_main/background:
    # "main", "fallback 1", "aux title_generation"); "" for a bare classify().
    route: str = ""

    @property
    def glyph(self) -> str:
        if self.label in {"ok", "confirmed"}:
            return "✅"
        if self.label == "block":
            return "⛔"
        return "⚠️"


def profile_scope(home: Any) -> str:
    """Profile identity a confirmation is scoped to (§3.7: row id + host + auth
    + profile + policy_rev): the captured Hermes home (``CapturedPaths.home``)
    as an absolute, normalised path string. Symlinks are NOT resolved, so the
    writer and the reader must both pass the captured home."""
    if home is None:
        return ""
    try:
        return os.path.normpath(os.path.abspath(os.path.expanduser(str(home))))
    except Exception:  # pragma: no cover - defensive
        return str(home)


def _row_hosts(row: dict[str, Any]) -> set[str]:
    match = row.get("match") or {}
    return {str(h).lower() for h in (match.get("base_url_hosts") or []) if isinstance(h, str) and h}


def _host_claimed(host: str, rows: list[dict[str, Any]]) -> bool:
    return bool(host) and any(host in _row_hosts(r) for r in rows)


def _auth_matches(row_auth: str, auth_hint: str | None) -> bool:
    if row_auth in (None, "", "any"):
        return True
    if auth_hint is None:
        return True  # unknown auth: candidates considered, worst wins (conservative)
    return row_auth == auth_hint


def _model_matches(row_models: list[str] | None, model: str | None) -> bool:
    if not row_models:
        return True
    if not model:
        return True  # unknown model: consider the row, worst wins
    model = str(model).lower()
    for pat in row_models:
        p = str(pat).lower()
        if model == p or model.startswith(p + "/") or model.startswith(p + "-") or model.endswith("/" + p):
            return True
    return False


def _host_known(host: str, rows: list[dict[str, Any]]) -> bool:
    if not host or _loopback_host(host):
        return True
    return _host_claimed(host, rows)


def _row_matches(row: dict[str, Any], *, host: str, provider: str | None, auth_hint: str | None,
                 model: str | None, api_mode: str | None, all_rows: list[dict[str, Any]] | None = None) -> bool:
    match = row.get("match") or {}
    hosts = _row_hosts(row)
    provider_ids = {p.lower() for p in (match.get("provider_ids") or [])}
    if hosts:
        if host not in hosts:
            return False
    else:
        # No curated host: provider-id match only, over an EMPTY or UNCLAIMED
        # host (audit fix — a provider id must never re-label a host another
        # row curates: 'opencode-zen' + api.moonshot.ai used to inherit kimi's
        # host with a better verdict). If the host is claimed, only the
        # claiming rows may match; a provider mismatch there means the
        # candidates set stays empty -> UNKNOWN/warn, never the host's policy.
        if not provider_ids or (provider or "").lower() not in provider_ids:
            return False
        if host and not _loopback_host(host):
            if _host_claimed(host, all_rows if all_rows is not None else [row]):
                return False  # claimed by a curated row: let it match instead
    # Host-listing rows: the HOST is authoritative — a request aimed at a
    # curated host gets that host's policy regardless of the provider label
    # ('custom' + api.moonshot.ai must see kimi's NONE_TRAINS, not UNKNOWN).
    if not _auth_matches(match.get("auth", "any"), auth_hint):
        return False
    api_modes = match.get("api_modes")
    if api_modes and api_mode and api_mode not in api_modes:
        return False
    return _model_matches(match.get("models"), model)


def _worst(polcies: list[str]) -> str:
    worst = "ALREADY"
    for p in polcies:
        if p in VERDICT_ORDER and VERDICT_ORDER.index(p) > VERDICT_ORDER.index(worst):
            worst = p
    return worst if polcies else "UNKNOWN"


def _bound_hosts(conf: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    hosts = conf.get("hosts")
    if isinstance(hosts, list):
        out |= {h.lower() for h in hosts if isinstance(h, str)}
    host = conf.get("host")
    if isinstance(host, str):
        out.add(host.lower())
    return out


def confirmation_valid(conf: Any, row: dict[str, Any], *, host: str, auth_hint: str | None,
                       policy_rev: str, profile: Any = None) -> bool:
    """One recorded confirmation attests ONE named route (§3.7): row id + host
    + auth + profile + policy_rev. Never a wildcard:

    - the record's own ``row_id`` must name this row (the dict key is not used);
    - only ACCOUNT_SETTING / ORG_AGREEMENT rows are confirmable;
    - host-curated rows: the request host is one of the row's hosts AND the
      record was bound to at least one of them (host aliases of the SAME row
      share it; an empty-host record never applies to a curated row);
    - provider-only rows: the request host must equal a bound host exactly
      ('' only matches the empty host — no promotion of arbitrary hosts);
    - auth: a recorded auth must equal the row's auth;
    - profile: when the caller passes ``profile``, the record must carry the
      same profile scope (legacy records without one are re-asked). Callers
      that pass no profile keep the pre-scope behaviour.
    """
    if not isinstance(conf, dict) or conf.get("revoked"):
        return False
    if str(conf.get("row_id") or "") != str(row.get("id") or "\x00"):
        return False
    if str(row.get("verdict")) not in CONFIRMABLE:
        return False
    if str(conf.get("policy_rev") or "") != str(policy_rev):
        return False
    row_auth = str((row.get("match") or {}).get("auth") or "any")
    conf_auth = conf.get("auth")
    if conf_auth not in (None, "") and row_auth != "any" and str(conf_auth) != row_auth:
        return False
    if auth_hint is not None and row_auth != "any" and auth_hint != row_auth:
        return False
    bound = _bound_hosts(conf)
    row_hosts = _row_hosts(row)
    if row_hosts:
        if host not in row_hosts or not (bound & row_hosts):
            return False
    elif host not in bound:
        return False
    if profile is not None:
        recorded = conf.get("profile")
        if not recorded or profile_scope(recorded) != profile_scope(profile):
            return False
    return True


def _per_request_label(flags: dict[str, Any], transform_implemented: bool | None,
                       reasons: list[str]) -> str:
    if transform_implemented is None:
        transform_implemented = _transform_implemented(flags)
    if transform_implemented:
        reasons.append("per-request flags applied by the plugin")
        return "ok-if-flagged"
    reasons.append("no per-request switch implemented for this route yet (P1)")
    return "warn"


def classify(provider: str | None, base_url: str | None, api_mode: str | None = None,
             model: str | None = None, auth_hint: str | None = None,
             confirmations: dict[str, Any] | None = None,
             transform_implemented: bool | None = None, *, profile: Any = None) -> RouteVerdict:
    """Classify one endpoint. Host is authoritative; a non-empty host this
    table does not list is never better than warn — neither a provider id nor
    a confirmation can vouch for it (NONE_* verdicts and flags are kept).

    ``profile`` (keyword-only): the captured Hermes home; scopes which recorded
    confirmations apply (see :func:`confirmation_valid`)."""
    table = load_table()
    rows = table.get("rows", [])
    rev = str(table.get("policy_rev"))
    host = host_of(base_url)
    loopback = _loopback_host(host)
    claimed = _host_claimed(host, rows)
    uncurated = bool(host) and not loopback and not claimed
    candidates = [r for r in rows if _row_matches(r, host=host, provider=provider, auth_hint=auth_hint,
                                                   model=model, api_mode=api_mode, all_rows=rows)]
    host_mismatch = False
    if not candidates and claimed:
        # Host authoritative (§3.7): a curated host whose rows all reject this
        # request's auth / api mode / model must not degrade to UNKNOWN — a
        # training host (kimi via an anthropic-compatible path with the
        # Claude Code marker) would escape its block. Worst of the host's
        # rows applies; positive labels are capped below.
        candidates = [r for r in rows if host in _row_hosts(r)]
        host_mismatch = True
    if not candidates:
        reasons = ["unrecognised endpoint (no curated route matches)"]
        return RouteVerdict(label="warn", policy="UNKNOWN", row_ids=[], reasons=reasons,
                            policy_rev=rev, host=host)
    policies = [str(r.get("verdict")) for r in candidates]
    worst = _worst(policies)
    row_ids = [str(r.get("id")) for r in candidates]
    flags: dict[str, Any] = {}
    account_setting = ""
    sources: list[str] = []
    ordered = [r for r in candidates if str(r.get("verdict")) == worst] + \
              [r for r in candidates if str(r.get("verdict")) != worst]
    for r in ordered:
        rf = r.get("flags")
        if isinstance(rf, dict) and rf and not flags:
            flags = dict(rf)
        if r.get("account_setting") and not account_setting:
            account_setting = str(r["account_setting"])
    for r in candidates:
        for s in r.get("sources") or []:
            url = s.get("url") if isinstance(s, dict) else None
            if isinstance(url, str) and url and url not in sources:
                sources.append(url)
    label = LABELS.get(worst, "warn")
    reasons: list[str] = []
    confirmed = False
    if worst == "PER_REQUEST":
        label = _per_request_label(flags, transform_implemented, reasons)
    elif worst in CONFIRMABLE:
        # Round-3: a confirmation promotes the label ONLY when EVERY matching
        # ACCOUNT_SETTING/ORG_AGREEMENT row is validly confirmed — confirming
        # one row (or a better/other row) never upgrades the worst one.
        confirmable = [r for r in candidates if str(r.get("verdict")) in CONFIRMABLE]
        records = list((confirmations or {}).values()) if isinstance(confirmations, dict) else []
        valid = {str(r.get("id")) for r in confirmable
                 if any(confirmation_valid(c, r, host=host, auth_hint=auth_hint, policy_rev=rev,
                                           profile=profile) for c in records)}
        if confirmable and all(str(r.get("id")) in valid for r in confirmable):
            confirmed = True
            reasons.append("you confirmed (not verified)")
            residual = [str(r.get("verdict")) for r in candidates
                        if str(r.get("verdict")) not in CONFIRMABLE]
            if residual and _worst(residual) == "PER_REQUEST":
                label = _per_request_label(flags, transform_implemented, reasons)  # flags still required
            else:
                label = "confirmed"
        else:
            label = "needs-confirmation"
            if account_setting:
                reasons.append(account_setting)
            if not all(confirmation_can_promote(r) for r in confirmable):
                reasons.append("setup does not record confirmations for a route without a curated "
                               "endpoint host")
            missing = [str(r.get("id")) for r in confirmable if str(r.get("id")) not in valid]
            if valid:
                reasons.append("still unconfirmed: " + ", ".join(missing))
    elif worst == "UNVERIFIED":
        reasons.append("retention/training policy unverified for this provider — "
                       "treat all chat content as retained")
    elif worst == "NONE_RETENTION":
        reasons.append("no training per cited sources, but the provider retains request data")
    cap_reason = ""
    if label in POSITIVE_LABELS or label == "needs-confirmation":
        if host_mismatch:
            cap_reason = "host-mismatch"
            reasons = [f"this request (auth / api mode / model) matches none of the curated rows for "
                       f"{host} — worst of that host's rows applies, capped at warn"]
        elif uncurated:
            # §3.7: an UNKNOWN/uncurated host never inherits a provider's
            # blessing — not ALREADY (round-#3: opencode id + attacker.example
            # got ✅), not PER_REQUEST, not a confirmation.
            cap_reason = "uncurated-host"
            reasons = [f"endpoint host {host} is not in the curated table — a provider id or a "
                       f"confirmation cannot vouch for it (capped at warn)"]
            if flags.get("kind") == "provider_routing":
                reasons.append("privacy flags are still added to each request")
        if cap_reason:
            label = "warn"
            confirmed = False
    elif uncurated and label != "block":
        reasons.append(f"endpoint host {host} is not in the curated table")
    if loopback and (label in POSITIVE_LABELS or label == "needs-confirmation"):
        # Audit fix: a loopback endpoint may be a proxy (LiteLLM, port-forward,
        # relay) forwarding to ANY remote provider — never report ✅/ALREADY,
        # and never suggest a confirmation could vouch for it.
        label = "warn"
        confirmed = False
        cap_reason = "loopback"
        reasons.append("local endpoint (forwarding unknown): data leaves the machine "
                       "wherever this proxy forwards — only treat as local if you run it")
    return RouteVerdict(label=label, policy=worst, row_ids=row_ids, flags=flags,
                        account_setting=account_setting, reasons=reasons,
                        policy_rev=rev, confirmed=confirmed, sources=sources, host=host,
                        cap_reason=cap_reason)


def _transform_implemented(flags: dict[str, Any]) -> bool:
    # provider_routing: OpenRouter/Nous flags (P0). verify_store_false: core already
    # sets Responses store:false; the plugin verifies it on the path (P0). p1 kinds: not yet.
    return flags.get("kind") in {"provider_routing", "verify_store_false"}


def confirmation_can_promote(row: dict[str, Any]) -> bool:
    """True when a setup confirmation of *row* can ever change an enforced
    label: a confirmable verdict on a row with curated hosts. Provider-only
    rows cannot — Hermes always sends to a concrete host and the table lists
    none for them, so the uncurated-host cap applies."""
    return str(row.get("verdict")) in CONFIRMABLE and bool(_row_hosts(row))


def _severity(v: RouteVerdict) -> tuple[int, int]:
    pol = VERDICT_ORDER.index(v.policy) if v.policy in VERDICT_ORDER else VERDICT_ORDER.index("UNVERIFIED")
    return (_LABEL_RANK.get(v.label, _LABEL_RANK["warn"]), pol)


def _runtime_endpoint_prediction(entry: dict[str, Any], v: RouteVerdict) -> RouteVerdict:
    """Status/setup prediction for a configured route with NO base_url whose
    matched rows are provider-only: Hermes resolves a concrete endpoint at
    request time, the table lists no host for these rows, and the request
    path (``classify`` with the resolved base_url) therefore caps it at warn.
    Showing ✅ here would contradict what enforcement does."""
    if host_of(entry.get("base_url")) or not v.row_ids:
        return v
    if v.label not in POSITIVE_LABELS and v.label != "needs-confirmation":
        return v
    by_id = {str(r.get("id")): r for r in load_table().get("rows", [])}
    if any(_row_hosts(by_id.get(rid, {})) for rid in v.row_ids):
        return v
    v.label = "warn"
    v.confirmed = False
    v.cap_reason = "runtime-endpoint"
    v.reasons = [f"no base_url configured: Hermes sends '{entry.get('provider') or '?'}' requests to an "
                 "endpoint it resolves at request time, which the curated table does not list — "
                 "requests are labelled ⚠️"]
    return v


def classify_main(facts: dict[str, Any], confirmations: dict[str, Any] | None = None,
                  auth_hint: str | None = None, *, profile: Any = None) -> RouteVerdict:
    """Coverage line 1: main chat requests = worst of main + configured fallbacks
    (§3.7), with implemented-transform awareness. Worst = most severe LABEL
    (what enforcement acts on), then policy."""
    main = facts.get("main") or {}
    candidates: list[RouteVerdict] = []
    if main:
        v = _runtime_endpoint_prediction(main, classify(
            main.get("provider"), main.get("base_url"), main.get("api_mode"), main.get("model"),
            auth_hint, confirmations, profile=profile))
        v.route = "main"
        candidates.append(v)
    for i, fb in enumerate(facts.get("fallbacks") or [], 1):
        v = _runtime_endpoint_prediction(fb, classify(
            fb.get("provider"), fb.get("base_url"), fb.get("api_mode"), fb.get("model"),
            None, confirmations, profile=profile))
        v.route = f"fallback {i}"
        candidates.append(v)
    if not candidates:
        return RouteVerdict(label="warn", policy="UNKNOWN", reasons=["no main model configured"])
    worst = max(candidates, key=_severity)  # first maximal: main wins ties
    seen_reasons: list[str] = []
    sources: list[str] = []
    for v in candidates:
        for r in v.reasons:
            if r not in seen_reasons:
                seen_reasons.append(r)
        for s in v.sources:
            if s not in sources:
                sources.append(s)
    worst.reasons = seen_reasons or worst.reasons
    worst.row_ids = sorted({rid for v in candidates for rid in v.row_ids})
    worst.sources = sources
    return worst


def classify_background(facts: dict[str, Any], confirmations: dict[str, Any] | None = None,
                        *, profile: Any = None) -> RouteVerdict:
    """Coverage line 2: background calls (titles, compression, vision, summary,
    task fallbacks) are NOT enforced — report the configured aux route's policy,
    else 'unknown (auto routing)' (§3.7)."""
    aux = facts.get("aux") or []
    runtime = facts.get("runtime")
    if not aux:
        return RouteVerdict(label="warn", policy="UNKNOWN", cap_reason="auto-routing",
                            reasons=["aux calls auto-route; the plugin cannot enforce them"])
    candidates = []
    for a in aux:
        v = _runtime_endpoint_prediction(a, classify(
            a.get("provider"), a.get("base_url"), None, a.get("model"), None, confirmations, profile=profile))
        v.route = f"aux {a.get('task') or '?'}"
        candidates.append(v)
    worst = max(candidates, key=_severity)
    if runtime:
        worst.reasons = [f"runtime '{runtime}' bypasses plugin middleware; the plugin cannot see this route"] + worst.reasons
    return worst


# Kept for importers; the rendered line names the ACTUAL surfaces (machine_line).
MACHINE_LINE = "history removed on close for the selected surfaces; some copies remain"


def normalize_surfaces(raw: Any) -> list[str]:
    """Lower-case, strip, de-duplicate (order kept); no validation."""
    out: list[str] = []
    items = raw.split(",") if isinstance(raw, str) else (raw if isinstance(raw, (list, tuple)) else [])
    for item in items:
        s = str(item).strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def machine_line(surfaces: list[str]) -> str:
    if not surfaces:
        return "history cleanup consented but no surfaces selected — nothing is removed"
    proof = " (end-to-end proven for CLI quit; other close paths unproven)" if "cli" in surfaces \
        else " (close paths on these surfaces are not end-to-end proven)"
    return (f"closed private chats removed from Hermes history on: {', '.join(surfaces)}{proof}; "
            "some copies remain")


def _plaintext_hosts(facts: dict[str, Any]) -> list[str]:
    """Main/fallback routes enforcement blocks as cleartext egress
    (enforce._plaintext_block_reason: http:// to a non-loopback host)."""
    out: list[str] = []
    for entry in [facts.get("main") or {}, *(facts.get("fallbacks") or [])]:
        base_url = entry.get("base_url") if isinstance(entry, dict) else None
        if not isinstance(base_url, str) or "://" not in base_url:
            continue
        try:
            parts = urlsplit(base_url)
            host = (parts.hostname or "").lower()
        except ValueError:
            continue
        if parts.scheme == "http" and host and not _loopback_host(host) and host not in out:
            out.append(host)
    return out


def coverage_lines(facts: dict[str, Any], confirmations: dict[str, Any] | None = None,
                   auth_hint: str | None = None, settings: dict[str, Any] | None = None,
                   cleanup_active: bool = False, *, surfaces: list[str] | None = None,
                   cleanup_note: str | None = None, profile: Any = None) -> list[str]:
    """The three separate coverage lines shown by status/setup/finalize (§3.7).

    ``cleanup_active`` must mean cleanup is EFFECTIVE (consent + surfaces +
    keep_history off + mode private); ``surfaces`` defaults to the settings'
    ``apply_to_platforms``; ``cleanup_note`` explains an inactive cleanup."""
    settings = settings or {}
    main = classify_main(facts, confirmations, auth_hint, profile=profile)
    bg = classify_background(facts, confirmations, profile=profile)
    mode_off = settings.get("mode") == "off"
    plaintext_enforced = not mode_off and not settings.get("allow_plaintext_remote", False)
    plaintext = _plaintext_hosts(facts) if plaintext_enforced else []
    main_host = host_of((facts.get("main") or {}).get("base_url"))
    main_plain = plaintext_enforced and bool(_plaintext_hosts({"main": facts.get("main")}))
    if main_plain:
        # enforce checks cleartext egress BEFORE any route verdict
        line1 = (f"⛔ cleartext http:// to {main_host} — requests are blocked "
                 "(allow_plaintext_remote off)")
    else:
        glyph = "⚠️" if mode_off else main.glyph
        line1 = f"{glyph} {main_label(main, settings)}"
        if main.route and main.route != "main":
            line1 += f" [worst route: {main.route}]"
    lines = [
        f"1. Main chat requests: {line1}",
        f"2. Background calls (titles, compression, vision): not enforced — {bg.glyph} {bg_label(bg)}",
    ]
    if cleanup_active:
        surf = normalize_surfaces(surfaces if surfaces is not None
                                  else settings.get("apply_to_platforms",
                                                    _SETTINGS_DEFAULTS["apply_to_platforms"]))
        lines.append(f"3. On this machine: {machine_line(surf)}")
    else:
        note = cleanup_note or "run `hermes incognito-private-mode setup`"
        lines.append(f"3. On this machine: history cleanup not enabled ({note})")
    for host in plaintext:
        if main_plain and host == main_host:
            continue
        lines.append(f"⛔ Cleartext http:// fallback route to {host}: requests to it are blocked "
                     "(allow_plaintext_remote is off).")
    if facts.get("memory_provider"):
        lines.append(f"⚠️ External memory provider '{facts['memory_provider']}' receives every message.")
    return lines


def route_details(facts: dict[str, Any], confirmations: dict[str, Any] | None = None,
                  auth_hint: str | None = None, *, profile: Any = None) -> list[str]:
    """Per configured route: matched row ids, policy, label and the rows'
    source links (status detail). Config facts only — no request content."""
    out: list[str] = []
    entries: list[tuple[str, dict[str, Any], str | None, bool]] = []
    if facts.get("main"):
        entries.append(("main", facts["main"], auth_hint, True))
    for i, fb in enumerate(facts.get("fallbacks") or [], 1):
        entries.append((f"fallback {i}", fb, None, True))
    for a in facts.get("aux") or []:
        entries.append((f"aux {a.get('task') or '?'}", a, None, False))
    for role, entry, hint, main_path in entries:
        v = classify(entry.get("provider"), entry.get("base_url"),
                     entry.get("api_mode") if main_path else None, entry.get("model"), hint,
                     confirmations, profile=profile)
        v = _runtime_endpoint_prediction(entry, v)
        host = host_of(entry.get("base_url")) or "(endpoint resolved by Hermes at request time)"
        rows = ", ".join(v.row_ids) or "none (unrecognised)"
        out.append(f"- {role}: {entry.get('provider') or '?'} @ {host} → rows: {rows}; "
                   f"policy {v.policy}; label {v.label}{' (you confirmed, not verified)' if v.confirmed else ''}")
        if v.sources:
            out.append("    sources: " + " · ".join(v.sources))
    return out


def _policy_phrase(v: RouteVerdict) -> str:
    cap = v.cap_reason
    if cap == "uncurated-host":
        return (f"endpoint host {v.host or '?'} is not in the curated table "
                "(a provider id or a confirmation cannot vouch for it)")
    if cap == "host-mismatch":
        return f"this request matches none of the curated rows for {v.host or '?'} (auth / api mode / model)"
    if cap == "runtime-endpoint":
        return "the endpoint Hermes resolves at request time is not in the curated table"
    if cap == "loopback":
        return "local endpoint — it may forward anywhere (forwarding unknown)"
    if cap == "auto-routing":
        return "unknown (auto routing)"
    prefix = "you confirmed the account setting (not verified by us); " if v.confirmed else ""
    if v.label == "ok":
        return "provider policy: no training (ALREADY, per cited sources; not verified by us)"
    if v.label == "confirmed":
        return "you confirmed the account setting (not verified by us)"
    if v.label == "ok-if-flagged":
        kind = v.flags.get("kind")
        if kind == "provider_routing":
            return prefix + "privacy flags (data_collection=deny, zdr) added to every request"
        if kind == "verify_store_false":
            return prefix + "Responses store:false checked on every request"
        return prefix + "per-request privacy flags applied"
    if v.label == "needs-confirmation":
        return "account setting needed — confirm it with `hermes incognito-private-mode setup`"
    if v.policy == "NONE_TRAINS":
        return "route trains on your data with no opt-out"
    if v.policy == "NONE_RETENTION":
        return "no training per cited sources, but the provider retains request data"
    if v.policy == "UNVERIFIED":
        return "retention/training policy unverified"
    if v.policy == "PER_REQUEST":
        return "per-request privacy switch not implemented for this route yet (P1)"
    if v.policy == "UNKNOWN":
        return "unrecognised endpoint (not in the curated table)"
    return "route not verified for privacy"


def main_label(v: RouteVerdict, settings: dict[str, Any]) -> str:
    """Line-1 text. Mirrors enforce.on_llm_execution exactly (pinned by a
    table-driven test): mode off → nothing applied; block + block_training →
    blocked; warn/needs-confirmation + not allow_unverified → blocked;
    defaults follow settings.DEFAULTS (allow_unverified / block_training True)."""
    allow_unverified = bool(settings.get("allow_unverified", True))
    block_training = bool(settings.get("block_training", True))
    phrase = _policy_phrase(v)
    if settings.get("mode") == "off":
        return ("Private Mode is off — requests are sent unchanged (no flags, no blocks) unless a chat "
                f"uses /private on; route: {phrase}")
    if v.label == "block":
        if block_training:
            return f"{phrase} — requests are blocked"
        return f"{phrase} — training block disabled (block_training off), requests are sent"
    if v.label in {"warn", "needs-confirmation"}:
        if allow_unverified:
            return f"{phrase} — requests are sent with this warning (allow_unverified on)"
        return f"{phrase} — requests are blocked (strict: allow_unverified off)"
    if v.label == "ok-if-flagged":
        return f"{phrase} — requests are sent (a request missing them is stopped)"
    return f"{phrase} — requests are sent"


def bg_label(v: RouteVerdict) -> str:
    if v.cap_reason == "auto-routing":
        return "unknown (auto routing)"
    if v.policy == "UNKNOWN":
        return "unrecognised endpoint on the configured aux route; the plugin cannot flag these calls"
    note = " (host not in the curated table)" if v.cap_reason in {"uncurated-host", "runtime-endpoint"} else ""
    return f"{v.policy}{note} on the configured aux route; the plugin cannot flag these calls"
