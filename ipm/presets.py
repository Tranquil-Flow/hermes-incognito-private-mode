"""Usability presets: two named profiles over the existing settings knobs.

- "complete": strictest coverage. Main-chat requests go to verified or
  you-confirmed routes only; memory blocks and the persona are stripped
  (best-effort, disclosed); memory/skills features blocked; history removed
  on close for CLI chats after setup consent. Background/auxiliary calls are
  disclosed, not enforced — see /private status for the full coverage lines.
- "provider-private": Hermes features fully on (memory saves, skill learning,
  history/search), main-chat requests locked down: verified or you-confirmed
  routes only, memory blocks + persona stripped from requests. Background
  calls disclosed, not enforced.

Presets are WRITERS: they set explicit, inspectable settings values. Consent
for history removal is never granted by a preset — the setup wizard asks.
"""
from __future__ import annotations

PRESETS: dict[str, dict[str, object]] = {
    "complete": {
        "mode": "private",
        "remove_history_on_close": True,
        "keep_history": False,
        "use_memory": False,
        "save_memory": False,
        "learn_skills": False,
        "allow_unverified": False,
        "allow_plaintext_remote": False,
        "strip_persona": True,
        "audit_log": True,
        "block_training": True,
        "deep_clean_reminder": "weekly",
    },
    "provider-private": {
        "mode": "private",
        "remove_history_on_close": False,
        "keep_history": True,
        "use_memory": False,
        "save_memory": True,
        "learn_skills": True,
        "allow_unverified": False,
        "allow_plaintext_remote": False,
        "strip_persona": True,
        "audit_log": True,
        "block_training": True,
        "deep_clean_reminder": "weekly",
    },
}

_PRESET_BLURBS = {
    "complete": "Complete — main-chat requests: verified or you-confirmed routes only, "
                "memory blocks + persona (best-effort) stripped; memory/skills/past-chat "
                "search blocked; history removed on close for CLI chats after setup "
                "consent; background calls disclosed, not enforced",
    "provider-private": "Provider-private — Hermes memory/skills/history features stay on; "
                        "main-chat requests: verified or you-confirmed routes only, memory "
                        "blocks + persona (best-effort) stripped; background calls "
                        "disclosed, not enforced",
}


def apply_preset(name: str, settings: dict) -> dict | None:
    """Return a NEW settings dict with the preset applied, or None if unknown."""
    preset = PRESETS.get(name)
    if not preset:
        return None
    out = dict(settings)
    out.update(preset)
    return out


def detect_preset(settings: dict) -> str | None:
    """Name of the preset whose values exactly match, else None (custom)."""
    for name, preset in PRESETS.items():
        if all(settings.get(k) == v for k, v in preset.items()):
            return name
    return None


def blurb(name: str) -> str:
    return _PRESET_BLURBS.get(name, "")
