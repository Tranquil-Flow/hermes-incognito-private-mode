"""Usability presets: two named profiles over the existing settings knobs.

- "complete": total privacy. Nothing stored by providers, nothing stored by
  Hermes chat history, memory/skills features blocked, strict providers.
- "provider-private": Hermes features fully on (memory saves, skill learning,
  history/search), but the model provider side is locked down: only providers
  with verified no-training behaviour (or per-request flags we apply), and
  Hermes-side extras (memory blocks, persona) are never sent.

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
    "complete": "Complete privacy — nothing kept by providers OR Hermes history; "
                "memory/skills features off; strict providers only",
    "provider-private": "Private providers — Hermes features fully on (memory, skills, "
                        "history/search); only verified no-training providers; Hermes-side "
                        "info (memory blocks, persona) never sent",
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
