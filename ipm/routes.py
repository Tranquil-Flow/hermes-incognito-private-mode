"""Route classification, worst-verdict, and the three-line coverage model
(BUILD_SPEC §3.7). Pure functions over routes.json + confirmations.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

    @property
    def glyph(self) -> str:
        if self.label in {"ok", "confirmed"}:
            return "✅"
        if self.label == "block":
            return "⛔"
        return "⚠️"


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
    for row in rows:
        match = row.get("match") or {}
        hosts = match.get("base_url_hosts") or []
        if host in {h.lower() for h in hosts}:
            return True
    return False


def _row_matches(row: dict[str, Any], *, host: str, provider: str | None, auth_hint: str | None,
                 model: str | None, api_mode: str | None, all_rows: list[dict[str, Any]] | None = None) -> bool:
    match = row.get("match") or {}
    hosts = {h.lower() for h in (match.get("base_url_hosts") or [])}
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
            for other in (all_rows if all_rows is not None else [row]):
                om = other.get("match") or {}
                if host in {h.lower() for h in (om.get("base_url_hosts") or [])}:
                    return False  # claimed by a curated row: let it match instead
    if provider_ids and (provider or "").lower() not in provider_ids:
        return False
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


def _confirmation_key(host: str, provider: str | None) -> tuple[str, str]:
    return (host, (provider or "").lower())


def classify(provider: str | None, base_url: str | None, api_mode: str | None = None,
             model: str | None = None, auth_hint: str | None = None,
             confirmations: dict[str, Any] | None = None,
             transform_implemented: bool | None = None) -> RouteVerdict:
    """Classify one endpoint. Host is authoritative; a host this table does not
    know is never blessed by a provider id (label ceiling warn)."""
    table = load_table()
    rows = table.get("rows", [])
    host = host_of(base_url)
    candidates = [r for r in rows if _row_matches(r, host=host, provider=provider, auth_hint=auth_hint,
                                                   model=model, api_mode=api_mode, all_rows=rows)]
    confirmed_ids: set[str] = set()
    if confirmations:
        for cid, conf in confirmations.items():
            if not isinstance(conf, dict):
                continue
            if str(conf.get("host") or "").lower() == host and \
               str(conf.get("provider") or "").lower() == (provider or "").lower() and \
               str(conf.get("policy_rev") or "") == str(table.get("policy_rev")):
                confirmed_ids.add(str(conf.get("row_id") or cid))
    if confirmed_ids:
        # A confirmation upgrades ONLY its ACCOUNT_SETTING/ORG_AGREEMENT row's
        # label (audit fix — narrowing candidates to the confirmed row dropped
        # worse co-candidates, e.g. a gemini-paid confirmation would have
        # hidden gemini-api NONE_TRAINS). Worst policy still wins.
        pass
    if not candidates:
        reasons = ["unrecognised endpoint (no curated route matches)"]
        return RouteVerdict(label="warn", policy="UNKNOWN", row_ids=[], reasons=reasons,
                            policy_rev=str(table.get("policy_rev")))
    policies = [str(r.get("verdict")) for r in candidates]
    worst = _worst(policies)
    row_ids = [str(r.get("id")) for r in candidates]
    confirmed = bool(confirmed_ids & {r.get("id") for r in candidates})
    flags: dict[str, Any] = {}
    account_setting = ""
    for r in candidates:
        rf = r.get("flags")
        if isinstance(rf, dict) and rf and not flags:
            flags = dict(rf)
        if r.get("account_setting") and not account_setting:
            account_setting = str(r["account_setting"])
    label = LABELS.get(worst, "warn")
    reasons: list[str] = []
    if worst == "PER_REQUEST":
        if transform_implemented is None:
            transform_implemented = _transform_implemented(flags)
        if transform_implemented:
            label = "ok-if-flagged"
            reasons.append("per-request flags applied by the plugin")
        else:
            label = "warn"
            reasons.append("no per-request switch implemented for this route yet (P1)")
    elif worst in {"ACCOUNT_SETTING", "ORG_AGREEMENT"}:
        label = "confirmed" if confirmed else "needs-confirmation"
        if confirmed:
            reasons.append("you confirmed (not verified)")
        elif account_setting:
            reasons.append(account_setting)
    elif worst == "UNKNOWN":
        reasons.append("no curated route matches this endpoint")
    if _loopback_host(host) and label in {"ok", "confirmed"}:
        # Audit fix: a loopback endpoint may be a proxy (LiteLLM, port-forward,
        # relay) forwarding to ANY remote provider — never report ✅/ALREADY.
        label = "warn"
        reasons.append("local endpoint (forwarding unknown): data leaves the machine "
                       "wherever this proxy forwards — only treat as local if you run it")
    return RouteVerdict(label=label, policy=worst, row_ids=row_ids, flags=flags,
                        account_setting=account_setting, reasons=reasons,
                        policy_rev=str(table.get("policy_rev")), confirmed=confirmed)


def _transform_implemented(flags: dict[str, Any]) -> bool:
    # provider_routing: OpenRouter/Nous flags (P0). verify_store_false: core already
    # sets Responses store:false; the plugin verifies it on the path (P0). p1 kinds: not yet.
    return flags.get("kind") in {"provider_routing", "verify_store_false"}


def classify_main(facts: dict[str, Any], confirmations: dict[str, Any] | None = None,
                  auth_hint: str | None = None) -> RouteVerdict:
    """Coverage line 1: main chat requests = worst of main + configured fallbacks
    (§3.7), with implemented-transform awareness."""
    main = facts.get("main") or {}
    candidates: list[RouteVerdict] = []
    if main:
        candidates.append(classify(main.get("provider"), main.get("base_url"), main.get("api_mode"),
                                   main.get("model"), auth_hint, confirmations))
    for fb in facts.get("fallbacks") or []:
        candidates.append(classify(fb.get("provider"), fb.get("base_url"), fb.get("api_mode"),
                                   fb.get("model"), None, confirmations))
    if not candidates:
        return RouteVerdict(label="warn", policy="UNKNOWN", reasons=["no main model configured"])
    worst = candidates[0]

    def _policy_index(v: RouteVerdict) -> int:
        return VERDICT_ORDER.index(v.policy) if v.policy in VERDICT_ORDER else 99

    for v in candidates[1:]:
        if _policy_index(v) > _policy_index(worst):
            worst = v
    # merge reasons from all candidate rows
    seen_reasons: list[str] = []
    for v in candidates:
        for r in v.reasons:
            if r not in seen_reasons:
                seen_reasons.append(r)
    worst.reasons = seen_reasons or worst.reasons
    worst.row_ids = sorted({rid for v in candidates for rid in v.row_ids})
    return worst


def classify_background(facts: dict[str, Any], confirmations: dict[str, Any] | None = None) -> RouteVerdict:
    """Coverage line 2: background calls (titles, compression, vision, summary,
    task fallbacks) are NOT enforced — report the configured aux route's policy,
    else 'unknown (auto routing)' (§3.7)."""
    aux = facts.get("aux") or []
    runtime = facts.get("runtime")
    if not aux:
        return RouteVerdict(label="warn", policy="UNKNOWN",
                            reasons=["aux calls auto-route; the plugin cannot enforce them"])
    candidates = [classify(a.get("provider"), a.get("base_url"), None, a.get("model"), None, confirmations)
                  for a in aux]
    worst = candidates[0]
    for v in candidates[1:]:
        vi = VERDICT_ORDER.index(v.policy) if v.policy in VERDICT_ORDER else 99
        wi = VERDICT_ORDER.index(worst.policy) if worst.policy in VERDICT_ORDER else 99
        if vi > wi:
            worst = v
    if runtime:
        worst.reasons = [f"runtime '{runtime}' bypasses plugin middleware; the plugin cannot see this route"] + worst.reasons
    return worst


MACHINE_LINE = "history removed on close; some copies remain"


def coverage_lines(facts: dict[str, Any], confirmations: dict[str, Any] | None = None,
                   auth_hint: str | None = None, settings: dict[str, Any] | None = None,
                   cleanup_active: bool = False) -> list[str]:
    """The three separate coverage lines shown by status/setup/finalize (§3.7)."""
    settings = settings or {}
    main = classify_main(facts, confirmations, auth_hint)
    bg = classify_background(facts, confirmations)
    lines = [
        f"1. Main chat requests: {main.glyph} {main_label(main, settings)}",
        f"2. Background calls (titles, compression, vision): not enforced — {bg.glyph} {bg_label(bg)}",
    ]
    if cleanup_active:
        lines.append(f"3. On this machine: {MACHINE_LINE}")
    else:
        lines.append("3. On this machine: history cleanup not enabled (run `hermes incognito-private-mode setup`)")
    if facts.get("memory_provider"):
        lines.append(f"⚠️ External memory provider '{facts['memory_provider']}' receives every message.")
    return lines


def main_label(v: RouteVerdict, settings: dict[str, Any]) -> str:
    if v.label == "ok-if-flagged":
        return "incognito-equivalent flags enforced on this request path"
    if v.label == "confirmed":
        return "you confirmed this account setting (not verified by us)"
    if v.label == "block":
        return "route trains with no opt-out — requests are blocked"
    if v.label == "needs-confirmation":
        return "account setting needed (see /private status detail)"
    return "unverified retention; allowed with a warning" if settings.get("allow_unverified") else "unverified; blocked"


def bg_label(v: RouteVerdict) -> str:
    if v.policy == "UNKNOWN":
        return "unknown (auto routing)"
    return f"{v.policy} on the configured aux route; the plugin cannot flag these calls"
