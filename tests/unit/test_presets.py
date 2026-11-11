"""Usability presets: bundles valid, detection exact, CLI applies explicitly."""
import time

import pytest


@pytest.fixture
def patch_capture(private_world, monkeypatch):
    from ipm import commands as commands_mod
    monkeypatch.setattr(commands_mod, "capture_paths", lambda: private_world)
    return private_world


def test_preset_keys_are_valid_settings():
    from ipm.presets import PRESETS
    from ipm.settings import DEFAULTS, VALID_MODES
    for name, preset in PRESETS.items():
        for key, value in preset.items():
            assert key in DEFAULTS, f"{name}.{key} not a real setting"
            if key == "mode":
                assert value in VALID_MODES
            elif key == "deep_clean_reminder":
                assert value in {"weekly", "monthly", "off"}
            else:
                assert isinstance(value, bool)


def test_complete_preset_shape():
    from ipm.presets import PRESETS
    p = PRESETS["complete"]
    assert p["save_memory"] is False and p["learn_skills"] is False
    assert p["keep_history"] is False and p["remove_history_on_close"] is True
    assert p["allow_unverified"] is False  # strict
    assert p["strip_persona"] is True


def test_provider_private_preset_shape():
    from ipm.presets import PRESETS
    p = PRESETS["provider-private"]
    assert p["save_memory"] is True and p["learn_skills"] is True
    assert p["keep_history"] is True and p["remove_history_on_close"] is False
    assert p["use_memory"] is False  # memory never SENT to the provider
    assert p["allow_unverified"] is False  # strict on the provider side
    assert p["strip_persona"] is True  # Hermes-side info never sent


def test_apply_and_detect_roundtrip(private_world):
    from ipm.presets import apply_preset, detect_preset
    from ipm.settings import DEFAULTS
    for name in PRESETS_EXPORT():
        applied = apply_preset(name, dict(DEFAULTS))
        assert detect_preset(applied) == name
    assert detect_preset(dict(DEFAULTS)) is None  # stock defaults = custom


def PRESETS_EXPORT():
    from ipm.presets import PRESETS
    return list(PRESETS.keys())


def test_cli_preset_applies_settings(patch_capture, capsys):
    from ipm import commands as commands_mod
    from ipm.settings import DEFAULTS, read_settings
    from hermes_cli.plugins_state import save_plugin_setting

    # preset writes persist to the shared harness config: snapshot & restore
    touched = list(__import__("ipm").presets.PRESETS["provider-private"].keys())
    saved = {k: DEFAULTS[k] for k in touched}
    try:
        class Args:
            ipm_command = "preset"
            preset_name = "provider-private"

        rc = commands_mod.cli_handler(Args, paths=patch_capture)
        assert rc == 0
        settings = read_settings(patch_capture)
        assert settings["save_memory"] is True
        assert settings["allow_unverified"] is False
        assert settings["strip_persona"] is True
        out = capsys.readouterr().out
        assert "provider-private" in out
    finally:
        for key, value in saved.items():
            save_plugin_setting("incognito-private-mode", (key,), value)


def test_cli_preset_never_grants_consent(patch_capture):
    # private_world seeds consent=True (post-setup world); the preset must not
    # CHANGE it — consent is only ever granted by the interactive wizard.
    from ipm import commands as commands_mod, store
    before = store.read("meta", patch_capture.data_dir).get("consent")

    class Args:
        ipm_command = "preset"
        preset_name = "complete"

    rc = commands_mod.cli_handler(Args, paths=patch_capture)
    assert rc == 0
    after = store.read("meta", patch_capture.data_dir).get("consent")
    assert after == before


def test_status_shows_profile_line(patch_capture, capsys):
    from ipm.presets import apply_preset, PRESETS
    from ipm.settings import DEFAULTS
    from hermes_cli.plugins_state import save_plugin_setting

    # the applied preset persists to the shared harness config: restore after
    saved = {k: DEFAULTS[k] for k in PRESETS["complete"].keys()}
    try:
        for key, value in apply_preset("complete", dict(DEFAULTS)).items():
            save_plugin_setting("incognito-private-mode", (key,), value)
        from ipm import commands as commands_mod

        class Args:
            ipm_command = "status"

        commands_mod.cli_handler(Args, paths=patch_capture)
        out = capsys.readouterr().out
        assert "Profile: complete" in out
    finally:
        for key, value in saved.items():
            save_plugin_setting("incognito-private-mode", (key,), value)
