"""Unit: settings/mode/platform scope, id validation, store flock concurrency (§6 row 2)."""

import json
import threading
import time

from conftest import FakePaths

from ipm import store
from ipm.settings import (
    DEFAULTS,
    cleanup_enrolled,
    effective_mode,
    platform_in_cleanup_scope,
    provider_controls_active,
    read_settings,
)


def test_defaults_match_spec():
    assert DEFAULTS["mode"] == "private"
    assert DEFAULTS["apply_to_platforms"] == ["cli", "tui", "desktop"]
    assert DEFAULTS["remove_history_on_close"] is True
    assert DEFAULTS["keep_history"] is False
    assert DEFAULTS["use_memory"] is False
    assert DEFAULTS["save_memory"] is False
    assert DEFAULTS["learn_skills"] is False
    assert DEFAULTS["allow_unverified"] is True
    assert DEFAULTS["block_training"] is True
    assert DEFAULTS["deep_clean_reminder"] == "weekly"


def test_read_settings_without_config(fake_paths):
    settings = read_settings(fake_paths)
    assert settings["mode"] == "private"


def test_effective_mode_per_chat_override(fake_paths):
    settings = dict(DEFAULTS)
    assert effective_mode(settings, None) == "private"
    assert effective_mode(settings, {"override": "keep"}) == "private"
    assert effective_mode(settings, {"override": "off"}) == "off"
    assert effective_mode(dict(settings, mode="off"), None) == "off"
    assert effective_mode(dict(settings, mode="off"), {"override": "keep"}) == "private"


def test_provider_controls_apply_on_all_platforms_while_private():
    settings = dict(DEFAULTS)
    assert provider_controls_active(settings, None) is True
    assert provider_controls_active(settings, {"override": "off"}) is False
    assert provider_controls_active(dict(settings, mode="off"), None) is False


def test_platform_scope_for_cleanup():
    settings = dict(DEFAULTS)
    assert platform_in_cleanup_scope("cli", settings) is True
    assert platform_in_cleanup_scope("desktop", settings) is True
    assert platform_in_cleanup_scope("telegram", settings) is False  # messaging: no cleanup (§3.10)
    assert platform_in_cleanup_scope("cron", settings) is False


def test_consent_gates_cleanup(fake_paths):
    settings = dict(DEFAULTS)
    meta = {}
    assert cleanup_enrolled(settings, meta) is False  # no setup -> no deletion (§3.9)
    meta = store.set_consent({}, recorded=False, now=time.time())
    assert cleanup_enrolled(settings, meta) is False
    meta = store.set_consent({}, recorded=True, now=time.time())
    assert cleanup_enrolled(settings, meta) is True
    assert cleanup_enrolled(dict(settings, remove_history_on_close=False), meta) is False


def test_session_id_validation():
    assert store.validate_session_id("abc-123_45") is True
    assert store.validate_session_id("a" * 200) is True
    assert store.validate_session_id("") is False
    assert store.validate_session_id("-leading") is False
    assert store.validate_session_id("bad id") is False
    assert store.validate_session_id("x" * 201) is False


def test_store_atomic_and_0600(fake_paths):
    def fn(data):
        data["k"] = "v"
        return data

    store.mutate("ledger", fake_paths.data_dir, fn)
    raw = (fake_paths.data_dir / "ledger.json").read_text()
    assert json.loads(raw) == {"k": "v"}
    import os
    mode = os.stat(fake_paths.data_dir / "ledger.json").st_mode & 0o777
    assert mode == 0o600


def test_store_flock_concurrency(fake_paths):
    """20 threads × 10 increments under the RMW lock: no lost updates (§6 row 2)."""

    def worker():
        for _ in range(10):
            def fn(data):
                data["n"] = data.get("n", 0) + 1
                return data
            store.mutate("ledger", fake_paths.data_dir, fn)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ledger = store.read("ledger", fake_paths.data_dir)
    assert ledger["n"] == 200


def test_lineage_hashing(fake_paths):
    from ipm.store import hash_session_id, lineage_hashes

    ledger = {
        hash_session_id("p"): {"state": "armed", "children": [hash_session_id("c")]},
        hash_session_id("c"): {"state": "armed", "parent_hash": hash_session_id("p")},
        hash_session_id("other"): {"state": "armed"},
    }
    lin = lineage_hashes(ledger, hash_session_id("p"))
    assert hash_session_id("p") in lin and hash_session_id("c") in lin
    assert hash_session_id("other") not in lin
