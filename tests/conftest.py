import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]  # tests/ -> plugin dir
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import os

# Data-safety gate (GOAL rules): tests run ONLY in the isolated harness home.
# Refuse to run against the real ~/.hermes so save_plugin_setting in tests can
# never touch the real config.
_HOME = os.environ.get("HOME", "")
if ".hermes/cache/scratch/ipm-harness" not in _HOME:
    raise SystemExit(
        "REFUSING: tests must run inside the harness. Source "
        "~/Projects/hermes-private-mode/harness/env.sh first (sets HOME, "
        "HERMES_HOME, HERMES_GATEWAY_LOCK_DIR). Real ~/.hermes stays untouched.")

# Core modules live at the checkout root (hermes_state.py, hermes_yaml.py, ...);
# the venv does not expose them without PYTHONPATH, which GOAL forbids — add the
# root explicitly (read-only use; tests only, never the plugin runtime).
_CORE_ROOT = "/Users/evinova-self/.hermes/hermes-agent"
if _CORE_ROOT not in sys.path:
    sys.path.append(_CORE_ROOT)

import pytest


class FakePaths:
    """CapturedPaths stand-in pointed at a temp profile."""

    def __init__(self, root: Path):
        self.home = root / "fakehome"
        self.db_path = self.home / "state.db"
        self.sessions_dir = self.home / "sessions"
        self.logs_dir = self.home / "logs"
        self.data_dir = root / "plugin-data"
        # Mirror CapturedPaths' memory-strip sources (contract-tested below so
        # the stand-in can never drift from production again).
        self.memory_path = self.home / "memories" / "MEMORY.md"
        self.user_profile_path = self.home / "memories" / "USER.md"
        self.home.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def fake_paths(tmp_path):
    return FakePaths(tmp_path)


@pytest.fixture
def private_world(fake_paths):
    """Ledger with consent recorded + a live arming generation, like post-setup."""
    import time
    from ipm import store

    def mutate_meta(m):
        m = store.set_consent(m, recorded=True, now=time.time())
        m = store.new_generation(m, armed=True, reason="test", now=time.time())
        return m

    store.mutate("meta", fake_paths.data_dir, mutate_meta)
    # Pre-seed last_sweep: hooks must never spawn racing daemon sweeps in tests.
    store.mutate("meta", fake_paths.data_dir,
                 lambda m: {**m, "last_sweep": {"at": time.time() + 3600}})
    return fake_paths
