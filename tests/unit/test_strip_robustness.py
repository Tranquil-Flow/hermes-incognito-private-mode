"""Robustness pins: separator-inside-entry, false-boundary, and plaintext-remote labels."""
import time

import pytest


def _render(entries, title="MEMORY (your personal notes) [9% — 200/2,200 chars]"):
    rule = "═" * 46
    body = "\n§\n".join(entries)
    return f"intro\n\n{rule}\n{title}\n{rule}\n{body}\n\nskills"


def test_entry_containing_separator_is_stripped_exactly():
    from ipm import strip
    entries = [
        "normal entry one",
        "tricky entry with separator inside:\n\npara2\n§\npara3 looks like a boundary",
        "SECRET-TAIL-ENTRY",
    ]
    body = "\n§\n".join(entries)
    wire = _render(entries)
    strip.set_memory_bodies([body])
    try:
        out, n, amb = strip.strip_memory_blocks_checked(wire)
        assert n == 1 and amb is False
        assert "para2" not in out and "para3" not in out
        assert "SECRET-TAIL-ENTRY" not in out
        assert "skills" in out  # no false boundary eating the rest
    finally:
        strip.reset_memory_bodies()


def test_unverified_provider_label_is_warn_not_block():
    from ipm.routes import classify
    v = classify("deepseek", "https://api.deepseek.com/v1", None, "api_key", None, None, None)
    assert v.label == "warn"
    assert any("unverified" in r for r in v.reasons)


def test_remote_plaintext_http_is_flagged():
    # a NON-loopback http:// endpoint must never be labeled ok/confirmed
    from ipm.routes import classify
    v = classify("custom", "http://insecure.example.net/v1", None, None, None, None, None)
    assert v.label != "ok"
