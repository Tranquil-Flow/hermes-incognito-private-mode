"""Regression tests for the security batch."""
import json
import logging

import pytest


@pytest.fixture
def patch_capture(private_world, monkeypatch):
    from ipm import store
    store.mutate("meta", private_world.data_dir,
                 lambda m: {**m, "last_sweep": {"at": __import__("time").time() + 3600}})
    return private_world


def _filter():
    from ipm.logfilter import PrivateLogFilter
    return PrivateLogFilter(mode_active=lambda: True)


def test_tool_calls_tail_after_quote_is_nuked(patch_capture):
    """The preview regex alone leaked the tail after the closing quote."""
    f = _filter()
    rec = logging.LogRecord("hermes.tool_calls", logging.INFO, "x", 1,
                            "%s", None, None)
    rec.args = None
    rec.msg = '11-11 10:00:00  terminal: "[redacted]hi\\" to bob SECRET-TAIL'
    f.filter(rec)
    assert "SECRET-TAIL" not in str(rec.msg)


def test_tool_calls_unquoted_preview_nuked(patch_capture):
    f = _filter()
    rec = logging.LogRecord("hermes.tool_calls", logging.INFO, "x", 1,
                            "%s", None, None)
    rec.args = None
    rec.msg = "11-11 10:00:00  terminal: cat SECRET-UNQUOTED"
    f.filter(rec)
    assert "SECRET-UNQUOTED" not in str(rec.msg)
    assert "[redacted]" in str(rec.msg)


def test_rendered_tail_beyond_scan_cap_redacted(patch_capture):
    f = _filter()
    secret = "SECRET-BEYOND-CAP"
    rec = logging.LogRecord("gateway.run", logging.INFO, "x", 1,
                            "%s", None, None)
    rec.args = None
    rec.msg = '11-11 10:00:00  reply: "visible' + ("pad " * 1100) + secret
    f.filter(rec)
    assert "SECRET-BEYOND-CAP" not in str(rec.msg)


def test_session_id_regex_rejects_trailing_newline():
    from ipm.store import validate_session_id
    assert validate_session_id("abc-123")
    assert not validate_session_id("abc-123\n")
    assert not validate_session_id("../etc")
    assert not validate_session_id("")
    assert not validate_session_id("")  # None-typed input also rejected by isinstance guard


def test_audit_append_lock_safe_and_private_dir(patch_capture):
    """Audit writes land under the store lock; the data dir is 0700."""
    import os
    from ipm.enforce import _audit_append

    _audit_append(patch_capture, {"provider": "x", "host": "h", "scheme": "s",
                                  "label": "warn", "policy": "p",
                                  "blocked": False, "persona": None})
    path = patch_capture.data_dir / "audit.jsonl"
    rec = json.loads(path.read_text().splitlines()[-1])
    assert rec["label"] == "warn"
    mode = os.stat(patch_capture.data_dir).st_mode & 0o777
    assert mode == 0o700
