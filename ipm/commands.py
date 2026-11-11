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

from . import lifecycle, presets, routes as routes_mod
from .settings import (
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

def status_text(paths=None) -> str:
    paths = paths or capture_paths()
    settings = read_settings(paths)
    from . import presets, store

    meta = store.read("meta", paths.data_dir)
    ledger = store.read("ledger", paths.data_dir)
    confirmations = store.read("confirmations", paths.data_dir)
    facts = route_detection_config(paths)
    enrolled = cleanup_enrolled(settings, meta)
    gen = store.current_generation(meta)
    counts: dict[str, int] = {}
    for entry in ledger.values():
        if isinstance(entry, dict):
            counts[entry.get("state", "?")] = counts.get(entry.get("state", "?"), 0) + 1
    mode_line = "private" if settings.get("mode") != "off" else "off"
    lines = [
        f"Private Mode: {'ON' if mode_line == 'private' else 'OFF'} — coverage, never a blanket Incognito:",
    ]
    profile = presets.detect_preset(settings)
    profile_line = f"Profile: {profile}" if profile else "Profile: custom"
    lines.append(profile_line)
    lines.extend(routes_mod.coverage_lines(facts, confirmations, None, settings, cleanup_active=enrolled))
    lines.append("")
    lines.append(f"History cleanup: {'enabled (consent recorded)' if enrolled else 'NOT enabled — run `hermes incognito-private-mode setup` to enable history removal'}")
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
    lines.append("scrollback, messages on messaging platforms, external memory providers.")
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

        set_override(str(session_id), sub, paths)
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
    ("claude", "Anthropic: turn OFF \"Help improve our AI models\" in Claude settings (Pro/Max)"),
    ("chatgpt", "ChatGPT: turn OFF \"Improve the model for everyone\" in settings"),
    ("codex", "Codex: turn OFF \"Include environments\" in Codex settings"),
    ("openrouter", "OpenRouter: review privacy settings at openrouter.ai/settings/privacy"),
    ("nous", "Nous Portal: enable Privacy Mode in the Portal"),
]


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

    print("Private Mode, honestly — guided setup.")
    print("Provider flags, memory stripping and tool vetoes are already active.")
    print("This wizard customises them and gates history removal. Nothing is deleted here.")
    print()
    settings = read_settings(paths)
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
    mode = _ask("Mode (private/off)", str(settings.get("mode", "private"))).lower()
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
    platforms = _ask("  Cleanup surfaces (cli,tui,desktop)", ",".join(settings.get("apply_to_platforms", ["cli", "tui", "desktop"])))
    reminder = _ask("  Deep-clean reminder (weekly/monthly/off)", str(settings.get("deep_clean_reminder", "weekly")))
    print()
    print("History removal consent — what REMOVAL does on your surfaces:")
    print("  Removes: closed private chats from Hermes history (sessions, messages, search index)")
    print("  via Hermes's own fenced delete. Whole lineages only (compression chains, delegate")
    print("  children). Chats created before setup, while off, or kept with /private keep survive.")
    print("  What STAYS: final replies kept for retries, CLI input history, pastes, deleted bytes")
    print("  in free DB pages until a deep clean, aux calls without our flags, logs from before")
    print("  this plugin loaded, and more — see /private status and the README \"What stays\" table.")
    consent = _ask("Enable removal of closed private chats from Hermes history? y/N", "n").lower()
    print()
    print("Detected routes and coverage (provider-side, informational):")
    facts = route_detection_config(paths)
    for line in routes_mod.coverage_lines(facts, {}, None, settings, cleanup_active=False):
        print(f"  {line}")
    print()
    print("One-time account settings (each confirmed here, shown as 'you confirmed (not verified)'):")
    confirmations: dict[str, Any] = {}
    for key, label in _ACCOUNT_CHECKLIST:
        answer = _ask(f"  {label} — done?", "n").lower()
        if answer.startswith("y"):
            confirmations[key] = {"confirmed_at": time.time(), "note": "confirmed in setup"}
    print()
    allow_plaintext = _ask("  Allow http:// to non-loopback hosts (cleartext)? y/N",
                           "y" if settings.get("allow_plaintext_remote", False) else "n").lower()
    strip_persona = _ask("  Strip your SOUL.md persona from prompts (best-effort)? y/N",
                         "y" if settings.get("strip_persona", False) else "n").lower()
    new_settings = {
        "mode": mode,
        "apply_to_platforms": [p.strip() for p in platforms.split(",") if p.strip() in {"cli", "tui", "desktop"}],
        "use_memory": use_memory.startswith("y"),
        "save_memory": save_memory.startswith("y"),
        "learn_skills": learn_skills.startswith("y"),
        "keep_history": keep_history.startswith("y"),
        "allow_unverified": allow_unverified.startswith("y"),
        "allow_plaintext_remote": allow_plaintext.startswith("y"),
        "strip_persona": strip_persona.startswith("y"),
        "audit_log": bool(settings.get("audit_log", True)),
        "block_training": block_training.startswith("y"),
        "deep_clean_reminder": reminder if reminder in {"weekly", "monthly", "off"} else "weekly",
    }

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
        meta = store.set_consent(meta, recorded=consent.startswith("y"), now=time.time())
        meta = store.new_generation(meta, armed=consent.startswith("y"), reason="setup", now=time.time())
        return meta

    def write_confirmations(existing: dict[str, Any]) -> dict[str, Any]:
        table = routes_mod.load_table()
        rev = table.get("policy_rev")
        for key, conf in confirmations.items():
            row = next((r for r in table.get("rows", []) if r.get("id") == key or key in str(r.get("id"))), None)
            host = ""
            provider = ""
            if row:
                match = row.get("match") or {}
                hosts = match.get("base_url_hosts") or []
                host = hosts[0] if hosts else ""
                provider = (match.get("provider_ids") or [""])[0]
            existing[key] = {"row_id": row.get("id") if row else key, "host": host,
                             "provider": provider, "policy_rev": rev, **conf}
        return existing

    store.mutate("meta", paths.data_dir, update_meta)
    store.mutate("confirmations", paths.data_dir, write_confirmations)
    print()
    print("Setup complete. Summary:")
    print(f"  mode={new_settings['mode']}  cleanup={'ENABLED' if consent.startswith('y') else 'disabled (no deletion will happen)'}")
    print(f"  surfaces={new_settings['apply_to_platforms']}  use_memory={new_settings['use_memory']}  save_memory={new_settings['save_memory']}")
    print(f"  learn_skills={new_settings['learn_skills']}  keep_history={new_settings['keep_history']}")
    print(f"  allow_unverified={new_settings['allow_unverified']}  block_training={new_settings['block_training']}")
    print(f"  confirmations recorded: {', '.join(sorted(confirmations)) or 'none'}")
    if not consent.startswith("y"):
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
                          help="complete = strict everywhere; provider-private = Hermes features on, providers locked down")


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
