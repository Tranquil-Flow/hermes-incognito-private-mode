"""``/private`` slash command and the ``hermes incognito-private-mode`` CLI
subcommand (BUILD_SPEC §3.9).

- ``/private status`` everywhere; mutating subcommands (keep/off/on/cleanup/
  deep-clean) on CLI/TUI/Desktop only — messaging replies "change settings on
  your own machine" (decision 3A; ``source.user_id`` is not authorization).
- ``hermes incognito-private-mode setup`` is TTY-only; without setup NO deletion
  ever happens (§3.9: first run = provider controls, strip, vetoes; no deletion).
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from . import lifecycle, presets, routes as routes_mod, store
from .settings import (
    VALID_PLATFORMS,
    capture_paths,
    cleanup_enrolled,
    read_settings,
    route_detection_config,
)

logger = logging.getLogger("ipm.commands")

MUTATING_HELP = "change settings on your own machine (run /private there)"
BLOCKED_TOOL_NAMES = {"memory", "skill_manage", "session_search"}


# --- status ------------------------------------------------------------------


def _audit_summary(paths: Any) -> str | None:
    """One-line summary of the last hour of content-free outbound audit records."""
    import json
    try:
        path = paths.data_dir / "audit.jsonl"
        if not path.exists():
            return None
        now = time.time()
        recent = []
        with open(path) as fh:
            for raw in fh.readlines()[-400:]:
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if now - float(rec.get("ts", 0)) <= 3600:
                    recent.append(rec)
        if not recent:
            return None
        blocked = sum(1 for rec in recent if rec.get("blocked"))
        labels: dict[str, int] = {}
        hosts: dict[str, int] = {}
        persona = 0
        for rec in recent:
            labels[str(rec.get("label", "?"))] = labels.get(str(rec.get("label", "?")), 0) + 1
            host = str(rec.get("host", "?"))
            hosts[host] = hosts.get(host, 0) + 1
            if rec.get("persona") == "stripped":
                persona += 1
        label_txt = ", ".join(f"{k}×{v}" for k, v in sorted(labels.items()))
        host_txt = ", ".join(sorted(h for h in hosts if h)[:3]) or "?"
        bits = [f"{len(recent)} call{'s' if len(recent) != 1 else ''} ({label_txt}; {blocked} blocked)",
                f"hosts: {host_txt}"]
        if any(rec.get("persona") == "present-not-located" for rec in recent):
            bits.append("⚠ persona present but not stripped")
        elif persona:
            bits.append(f"persona stripped ×{persona}")
        return "; ".join(bits)
    except Exception:
        return None

def cleanup_state(settings: dict[str, Any], meta: dict[str, Any]) -> tuple[bool, str]:
    """EFFECTIVE history cleanup, mirroring lifecycle.observe_session_start's
    arming rule: consent recorded AND remove_history_on_close AND a non-empty
    surface list AND keep_history off AND mode private. Returns (enabled, why)."""
    surfaces = routes_mod.normalize_surfaces(settings.get("apply_to_platforms"))
    consent = meta.get("consent") if isinstance(meta, dict) else None
    if not (isinstance(consent, dict) and consent.get("recorded") is True):
        return False, ("no consent recorded — run `hermes incognito-private-mode setup` "
                       "to enable history removal")
    if not cleanup_enrolled(settings, meta):
        return False, "remove_history_on_close is off"
    if not surfaces:
        return False, "no cleanup surfaces selected (apply_to_platforms is empty) — nothing is removed"
    if settings.get("keep_history"):
        return False, "keep_history is on — chats are kept in Hermes history"
    if settings.get("mode") == "off":
        return False, "mode is off — new chats are kept"
    return True, f"enabled on: {', '.join(surfaces)} (consent recorded)"


def _confirmation_lines(confirmations: dict[str, Any], profile: str) -> list[str]:
    """Recorded account confirmations that can change a label, split into
    those that apply to this profile + policy revision and those that do not."""
    table = routes_mod.load_table()
    rev = str(table.get("policy_rev"))
    by_id = {str(r.get("id")): r for r in table.get("rows", [])}
    active: list[str] = []
    stale: list[str] = []
    for rec in (confirmations or {}).values():
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("row_id") or "")
        row = by_id.get(rid)
        if row is None or not routes_mod.confirmation_can_promote(row):
            continue  # inert record: it never changes any label
        scoped = bool(rec.get("profile")) and routes_mod.profile_scope(rec.get("profile")) == profile
        (active if scoped and str(rec.get("policy_rev")) == rev else stale).append(rid)
    lines: list[str] = []
    if active:
        lines.append("Account confirmations (you confirmed, not verified): " + ", ".join(sorted(set(active))))
    if stale:
        lines.append("Confirmations NOT applied (policy revision changed, other profile, or recorded "
                     "before scoping) — re-run setup: " + ", ".join(sorted(set(stale))))
    return lines


def status_text(paths=None) -> str:
    paths = paths or capture_paths()
    settings = read_settings(paths)
    from . import presets, store

    meta = store.read("meta", paths.data_dir)
    ledger = store.read("ledger", paths.data_dir)
    confirmations = store.read("confirmations", paths.data_dir)
    facts = route_detection_config(paths)
    profile = routes_mod.profile_scope(paths.home)
    cleanup_on, cleanup_why = cleanup_state(settings, meta)
    gen = store.current_generation(meta)
    counts: dict[str, int] = {}
    for entry in ledger.values():
        if isinstance(entry, dict):
            counts[entry.get("state", "?")] = counts.get(entry.get("state", "?"), 0) + 1
    mode_line = "private" if settings.get("mode") != "off" else "off"
    lines = [
        f"Private Mode: {'ON' if mode_line == 'private' else 'OFF'} — coverage, never a blanket Incognito:",
    ]
    profile_name = presets.detect_preset(settings)
    lines.append(f"Profile: {profile_name}" if profile_name else "Profile: custom")
    lines.extend(routes_mod.coverage_lines(facts, confirmations, None, settings,
                                           cleanup_active=cleanup_on,
                                           cleanup_note=None if cleanup_on else cleanup_why,
                                           profile=profile))
    details = routes_mod.route_details(facts, confirmations, None, profile=profile)
    if details:
        lines.append("Routes (matched policy rows and their sources):")
        lines.extend(f"  {d}" for d in details)
    lines.extend(_confirmation_lines(confirmations, profile))
    lines.append("")
    lines.append(f"History cleanup: {cleanup_why if cleanup_on else 'NOT enabled — ' + cleanup_why}")
    if gen:
        lines.append(f"Arming generation: #{gen.get('n')} ({'armed' if gen.get('armed') else 'disarmed'})")
    lines.append(f"Sessions: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none tracked"))
    last = meta.get("last_sweep") or {}
    if isinstance(last, dict) and last.get("at"):
        ago = int(time.time() - float(last["at"]))
        summary = ", ".join(f"{k}={v}" for k, v in sorted(last.items()) if k != "at" and v)
        lines.append(f"Last sweep: {ago}s ago ({summary or 'nothing to do'})")
    reminder = settings.get("deep_clean_reminder", "weekly")
    if reminder != "off":
        lines.append(f"Deep-clean reminder: {reminder} — deleted bytes remain in free DB pages until a deep clean.")
    if settings.get("audit_log", True):
        audit = _audit_summary(paths)
        if audit:
            lines.append(f"Outbound audit (last hour): {audit}")
    lines.append("")
    lines.append("What stays (never removed by this plugin): final replies kept for retries (by Hermes design),")
    lines.append("CLI input history (~/.hermes/.hermes_history), paste files, interrupt_debug.log, deleted bytes in")
    lines.append("free DB pages until a deep clean, auxiliary calls without our flags, max-iterations summary call,")
    lines.append("codex_app_server runtime, safe-mode sessions, gateway shutdown snapshots/spools, save_trajectories,")
    lines.append("spawn-ledger.json for -q, logs written before this plugin loaded, backups, SSD remanence, terminal")
    lines.append("scrollback, messages on messaging platforms, external memory providers, and the earlier parts")
    lines.append("of compressed chats (compression roots).")
    lines.append("Commands: /private status · keep · on · off · cleanup · deep-clean")
    return "\n".join(lines)


# --- slash command -----------------------------------------------------------

def handle_private_command(raw_args: str, *, session_id: str | None = None, platform: str | None = None,
                           **_kw: Any) -> str:
    args = (raw_args or "").strip().split()
    sub = (args[0] if args else "status").lower()
    paths = capture_paths()
    if sub in ("", "status"):
        return status_text(paths)
    if platform and platform not in {"cli", "tui", "desktop"}:
        return MUTATING_HELP  # messaging: status only (decision 3A)
    if sub in ("keep", "off", "on"):
        if not session_id:
            return "Private mode: no active session to change."
        from .lifecycle import set_override

        if not set_override(str(session_id), sub, paths):
            return "Private mode: could not record that override (invalid session id)."
        if sub == "keep":
            from .store import hash_session_id
            entry = store.read("ledger", paths.data_dir).get(hash_session_id(str(session_id)))
            if isinstance(entry, dict) and entry.get("state") == "deleted":
                return "Private mode: this chat was already removed from Hermes history."
        word = {"keep": "kept in history", "off": "off for this chat", "on": "on for this chat"}[sub]
        return f"Private mode {word}."
    if sub == "cleanup":
        outcomes = lifecycle.cleanup_outcomes(paths)
        return "Private cleanup:\n" + "\n".join(outcomes)
    if sub == "deep-clean":
        return lifecycle.deep_clean_report(paths)
    return "Usage: /private status|keep|on|off|cleanup|deep-clean"


# --- setup wizard (TTY only, §3.9) -------------------------------------------

_ACCOUNT_CHECKLIST = [
    # (key, routes.json row id, prompt) — row ids are EXPLICIT (substring
    # matching never bound; 2026-11-11 adversarial round). Only rows whose
    # confirmation can change an enforced label are ASKED (round-3): see
    # _checklist_plan / routes.confirmation_can_promote. A listed row that
    # cannot be promoted (e.g. Nebius: no curated endpoint host) is explained,
    # never asked — a recorded "yes" there would change nothing.
    ("claude", "anthropic-oauth",
     "Anthropic (Claude Pro/Max sign-in): turn OFF \"Help improve our AI models\" in Claude settings"),
    ("nebius", "nebius",
     "Nebius Token Factory: confirm org Zero-Data-Retention in the console"),
]

# Informational only: these routes' labels do NOT depend on a confirmation
# (PER_REQUEST flags are enforced per request; Codex stays UNVERIFIED).
_ACCOUNT_TIPS = [
    "ChatGPT/Codex: turn OFF \"Improve the model for everyone\" and \"Include environments\" "
    "(the Codex route stays ⚠️ UNVERIFIED: retention is undocumented)",
    "OpenRouter: review openrouter.ai/settings/privacy (the plugin already sends "
    "data_collection=deny + zdr on every main-chat request)",
    "Nous Portal: enable Privacy Mode (the plugin already sends data_collection=deny + zdr "
    "on every main-chat request)",
]


def _configured_route_hosts(row: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    """Hosts of configured routes (main, fallbacks, aux) whose provider is one
    of *row*'s provider ids ('' = endpoint resolved by Hermes)."""
    ids = {str(p).lower() for p in ((row.get("match") or {}).get("provider_ids") or [])}
    out: list[str] = []
    for entry in [facts.get("main") or {}, *(facts.get("fallbacks") or []), *(facts.get("aux") or [])]:
        if isinstance(entry, dict) and str(entry.get("provider") or "").lower() in ids:
            host = routes_mod.host_of(entry.get("base_url"))
            if host not in out:
                out.append(host)
    return out


def _checklist_plan(facts: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any], str]], list[str]]:
    """(rows to ask, honest notes for listed rows that are not asked)."""
    table = routes_mod.load_table()
    by_id = {str(r.get("id")): r for r in table.get("rows", [])}
    ask: list[tuple[str, dict[str, Any], str]] = []
    notes: list[str] = []
    for key, row_id, prompt in _ACCOUNT_CHECKLIST:
        row = by_id.get(row_id)
        name = prompt.split(":")[0]
        if row is None:
            notes.append(f"{name}: not asked — route row '{row_id}' is missing from the policy table.")
            continue
        if routes_mod.confirmation_can_promote(row):
            ask.append((key, row, prompt))
            continue
        hosts = _configured_route_hosts(row, facts)
        where = ", ".join(h or "an endpoint Hermes resolves at request time" for h in hosts) \
            or "no such route is configured"
        notes.append(f"{name}: not asked — the policy table lists no endpoint host for this route, so a "
                     f"confirmation cannot vouch for where requests go ({where}); it stays ⚠️ unconfirmed.")
    return ask, notes


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
        return answer or default
    except EOFError:
        return default


def run_setup(paths=None) -> int:
    """Guided one-time setup. Non-TTY refuses cleanly (unattended runs, rule 12)."""
    paths = paths or capture_paths()
    if not _is_tty():
        print("setup needs an interactive terminal (TTY). Run it in your own terminal:")
        print("  hermes incognito-private-mode setup")
        return 2
    from . import store

    profile = routes_mod.profile_scope(paths.home)
    settings = read_settings(paths)
    print("Private Mode, honestly — guided setup.")
    if settings.get("mode") == "off":
        print("Private Mode is currently OFF (mode=off): provider flags, memory stripping, route blocks and")
        print("tool vetoes are not applied — except in chats switched on with /private on.")
    else:
        print("Current mode=private: provider flags (on supported routes), memory stripping, route blocks and")
        print("tool vetoes apply to every chat except chats switched off with /private off.")
    print("This wizard customises them and gates history removal. Nothing is deleted here.")
    print()
    print("Pick a starting point (Enter = 1):")
    print("  1) " + presets.blurb("complete"))
    print("  2) " + presets.blurb("provider-private"))
    print("  3) Custom — answer each question (defaults shown)")
    choice = _ask("Profile", "1")
    preset_name = {"1": "complete", "2": "provider-private"}.get(choice.strip())
    if preset_name:
        settings = presets.apply_preset(preset_name, settings)
        print(f"  -> {preset_name} preset applied to the defaults below (you can still change any answer).")
        print(f"  Note: history removal consent is asked separately and is never pre-granted.")
    mode = _ask("Mode (private/off)", str(settings.get("mode", "private"))).strip().lower()
    if mode not in {"private", "off"}:
        mode = "private"
    print("Toggles — Enter keeps the default. One-line explanations:")
    use_memory = _ask("  Read existing memory into requests? (memory blocks are stripped when no) y/N",
                      "n" if not settings.get("use_memory", False) else "y").lower()
    save_memory = _ask("  Let the model save memory? y/N", "n" if not settings.get("save_memory", False) else "y").lower()
    learn_skills = _ask("  Let the model learn skills? y/N", "n" if not settings.get("learn_skills", False) else "y").lower()
    keep_history = _ask("  Keep chats in Hermes history (session_search on)? y/N",
                        "y" if settings.get("keep_history", False) else "n").lower()
    allow_unverified = _ask("  Allow routes with unverified retention (warned)? Y/n",
                            "y" if settings.get("allow_unverified", True) else "n").lower()
    block_training = _ask("  Block routes that train with no opt-out? Y/n",
                          "y" if settings.get("block_training", True) else "n").lower()
    allow_plaintext = _ask("  Allow http:// to non-loopback hosts (cleartext)? y/N",
                           "y" if settings.get("allow_plaintext_remote", False) else "n").lower()
    strip_persona = _ask("  Strip your SOUL.md persona from prompts (best-effort)? y/N",
                         "y" if settings.get("strip_persona", False) else "n").lower()
    raw_platforms = _ask("  Cleanup surfaces (cli,tui,desktop)",
                         ",".join(settings.get("apply_to_platforms", ["cli"])))
    typed = routes_mod.normalize_surfaces(raw_platforms)
    surfaces = [p for p in typed if p in VALID_PLATFORMS]
    ignored = [p for p in typed if p not in VALID_PLATFORMS]
    if ignored:
        print(f"  ignored surface(s): {', '.join(ignored)} (valid: cli, tui, desktop)")
    reminder = _ask("  Deep-clean reminder (weekly/monthly/off)",
                    str(settings.get("deep_clean_reminder", "weekly"))).strip().lower()
    print()
    print(f"History removal consent — what REMOVAL does on the surfaces you selected "
          f"({', '.join(surfaces) or 'none'}):")
    print("  Removes: a closed private chat's own session row, its messages and search-index entries,")
    print("  via Hermes's fenced delete — only chats created after this consent while mode=private.")
    print("  These are never removed: the earlier part of a compressed chat (its compression root stays")
    print("  in Hermes history; status reports it), chats created before setup, while off, kept with")
    print("  /private keep, or while keep_history is on.")
    print("  Removal is proven end-to-end only for CLI chats closed by quitting the CLI; /new inside the")
    print("  CLI and TUI/Desktop closes use the same removal step but are not proven end-to-end.")
    print("  What STAYS: final replies kept for retries, CLI input history, pastes, deleted bytes")
    print("  in free DB pages until a deep clean, aux calls without our flags, logs from before")
    print("  this plugin loaded, and more — see /private status and the README \"What stays\" table.")
    consent = _ask("Enable removal of closed private chats from Hermes history? y/N", "n").lower()
    consent_yes = consent.startswith("y")
    new_settings = {
        "mode": mode,
        "apply_to_platforms": surfaces,
        "use_memory": use_memory.startswith("y"),
        "save_memory": save_memory.startswith("y"),
        "learn_skills": learn_skills.startswith("y"),
        "keep_history": keep_history.startswith("y"),
        "allow_unverified": allow_unverified.startswith("y"),
        "allow_plaintext_remote": allow_plaintext.startswith("y"),
        "strip_persona": strip_persona.startswith("y"),
        "audit_log": bool(settings.get("audit_log", True)),
        "block_training": block_training.startswith("y"),
        "remove_history_on_close": consent_yes,
        "deep_clean_reminder": reminder if reminder in {"weekly", "monthly", "off"} else "weekly",
    }
    print()
    print("Detected routes and coverage with these answers (provider-side, informational):")
    facts = route_detection_config(paths)
    existing_confs = store.read("confirmations", paths.data_dir)
    preview_on, preview_why = cleanup_state(new_settings, {"consent": {"recorded": consent_yes}})
    for line in routes_mod.coverage_lines(facts, existing_confs, None, new_settings,
                                          cleanup_active=preview_on,
                                          cleanup_note=None if preview_on else preview_why,
                                          profile=profile):
        print(f"  {line}")
    for line in routes_mod.route_details(facts, existing_confs, None, profile=profile):
        print(f"  {line}")
    print()
    ask_rows, notes = _checklist_plan(facts)
    answers: dict[str, tuple[dict[str, Any], bool]] = {}
    if ask_rows:
        print("Account settings a confirmation can change (recorded as 'you confirmed (not verified)';")
        print("answering n revokes an earlier confirmation for this profile):")
        for key, row, prompt in ask_rows:
            answer = _ask(f"  {prompt} — done? y/N", "n").lower()
            answers[key] = (row, answer.startswith("y"))
    for note in notes:
        print(f"  {note}")
    print("Recommended account settings (informational — the plugin cannot see them and they do not "
          "change any route label):")
    for tip in _ACCOUNT_TIPS:
        print(f"  - {tip}")

    from hermes_cli.plugins_state import save_plugin_setting  # public allowlist; THE settings writer

    try:
        for key, value in new_settings.items():
            save_plugin_setting("incognito-private-mode", (key,), value)
    except PermissionError as exc:
        print(f"Settings are managed on this install: {exc}")
        print("Aborting without recording consent.")
        return 1
    except Exception as exc:
        print(f"Could not write settings ({type(exc).__name__}). Aborting without consent.")
        return 1

    def update_meta(meta: dict[str, Any]) -> dict[str, Any]:
        meta = store.set_consent(meta, recorded=consent_yes, now=time.time())
        # Arm only when consent AND mode=private: chats created while off
        # survive forever (lifecycle opens a new generation on a mode flip).
        meta = store.new_generation(meta, armed=consent_yes and mode == "private",
                                    reason="setup", now=time.time())
        return meta

    recorded: list[str] = []
    revoked: list[str] = []
    pruned: list[str] = []

    def write_confirmations(existing: dict[str, Any]) -> dict[str, Any]:
        """This profile's confirmations become exactly the answers given now:
        yes -> (re)recorded with full scope; no -> any earlier record for that
        row in this profile is REVOKED; records that can never change a label
        (old OpenRouter/Nous/Codex/Nebius entries) are removed."""
        table = routes_mod.load_table()
        rev = str(table.get("policy_rev"))
        by_id = {str(r.get("id")): r for r in table.get("rows", [])}
        asked = {str(row.get("id")): yes for row, yes in answers.values()}
        out: dict[str, Any] = {}
        for key, rec in (existing if isinstance(existing, dict) else {}).items():
            if not isinstance(rec, dict):
                pruned.append(str(key))
                continue
            rid = str(rec.get("row_id") or "")
            row = by_id.get(rid)
            if row is None or not routes_mod.confirmation_can_promote(row):
                pruned.append(rid or str(key))
                continue
            rec_profile = routes_mod.profile_scope(rec.get("profile")) if rec.get("profile") else None
            if rid in asked and rec_profile in (None, profile):
                if not asked[rid] and rid not in revoked:
                    revoked.append(rid)
                continue  # replaced below (yes) or revoked (no)
            out[str(key)] = rec  # another profile's record / unasked row: untouched
        for key, (row, yes) in answers.items():
            if not yes:
                continue
            match = row.get("match") or {}
            hosts = sorted({str(h).lower() for h in (match.get("base_url_hosts") or []) if h})
            out[key] = {
                "row_id": str(row.get("id")),
                "host": hosts[0] if hosts else "",
                "hosts": hosts,
                "auth": str(match.get("auth") or "any"),
                "provider": str((match.get("provider_ids") or [""])[0]),
                "profile": profile,
                "policy_rev": rev,
                "confirmed_at": time.time(),
                "note": "confirmed in setup (not verified)",
            }
            recorded.append(str(row.get("id")))
        return out

    store.mutate("meta", paths.data_dir, update_meta)
    store.mutate("confirmations", paths.data_dir, write_confirmations)
    print()
    print("Setup complete. Summary:")
    enabled_now, why_now = cleanup_state(read_settings(paths), store.read("meta", paths.data_dir))
    cleanup_txt = f"ENABLED ({why_now})" if enabled_now else f"disabled ({why_now})"
    print(f"  mode={new_settings['mode']}  cleanup={cleanup_txt}")
    print(f"  surfaces={new_settings['apply_to_platforms']}  use_memory={new_settings['use_memory']}  save_memory={new_settings['save_memory']}")
    print(f"  learn_skills={new_settings['learn_skills']}  keep_history={new_settings['keep_history']}")
    print(f"  allow_unverified={new_settings['allow_unverified']}  block_training={new_settings['block_training']}")
    print(f"  confirmations recorded: {', '.join(recorded) or 'none'}")
    if revoked:
        print(f"  confirmations revoked (you answered n): {', '.join(revoked)}")
    if pruned:
        print(f"  old confirmations removed (they never changed any label): {', '.join(sorted(set(pruned)))}")
    if not consent_yes:
        print("  Without consent, nothing is ever deleted from Hermes history.")
    print("  Run /private status any time.")
    return 0


# --- CLI subcommand ----------------------------------------------------------

def cli_register(setup_fn) -> None:
    """setup_fn receives THIS plugin's argparse parser (main.py builds it named
    ``incognito-private-mode``); we add our own subcommand tree onto it."""
    setup_fn.help = "Private Mode, honestly: status, cleanup, deep-clean, setup"
    setup_fn.description = "Private Mode, honestly (provider flags, memory strip, vetoes, consent-gated history removal)"
    sub = setup_fn.add_subparsers(dest="ipm_command")
    sub.add_parser("status", help="Show mode, coverage lines, routes, queue, what stays")
    sub.add_parser("cleanup", help="Run one sweep now and print per-session outcomes (hashed ids)")
    sub.add_parser("deep-clean", help="Show DB size, free disk and the manual deep-clean steps")
    sub.add_parser("setup", help="Guided setup (TTY only); records cleanup consent")
    preset_p = sub.add_parser("preset", help="Apply a settings profile (non-interactive)")
    preset_p.add_argument("preset_name", choices=["complete", "provider-private"],
                          help="complete = strictest coverage; provider-private = Hermes features on, main-chat requests locked down (see /private status)")


def cli_handler(args, **_kw: Any) -> int:
    paths = capture_paths()
    command = getattr(args, "ipm_command", "status") or "status"
    if command == "setup":
        return run_setup(paths)
    if command == "preset":
        name = getattr(args, "preset_name", "") or getattr(args, "name", "")
        applied = presets.apply_preset(name, read_settings(paths))
        if applied is None:
            print(f"unknown preset: {name} (choose complete | provider-private)")
            return 1
        from hermes_cli.plugins_state import save_plugin_setting
        try:
            for key, value in presets.PRESETS[name].items():
                save_plugin_setting("incognito-private-mode", (key,), value)
        except PermissionError as exc:
            print(f"Settings are managed on this install: {exc}")
            return 1
        print(f"Applied preset '{name}': " + presets.blurb(name))
        print("History removal still needs consent: hermes incognito-private-mode setup")
        return 0
    if command == "cleanup":
        print("\n".join(lifecycle.cleanup_outcomes(paths)))
        return 0
    if command == "deep-clean":
        print(lifecycle.deep_clean_report(paths))
        return 0
    print(status_text(paths))
    return 0
