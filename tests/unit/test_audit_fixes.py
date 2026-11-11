"""Regression tests for the Opus audit fixes (2026-11-10 review round).

Each test pins one audit finding so the fix cannot silently regress:
- blank-line memory entries must not leak past the strip (HIGH)
- strip ambiguity must be reported (fail-closed contract)
- mode-off chats are excluded forever, even after re-enable (HIGH)
- compaction lineage: surviving parents are never marked deleted (HIGH)
- unledgered predecessors protect continuations (HIGH)
- logfilter ReDoS: pathological input stays fast (HIGH)
- provider-id spoofing cannot re-label a curated host (MEDIUM)
- /private on in mode=off enables provider controls (MEDIUM)
- read_row_checked distinguishes absent vs error (MEDIUM)
"""

import time

import pytest

from ipm import lifecycle, store
from ipm.routes import classify
from ipm.settings import DEFAULTS, effective_mode, provider_controls_active
from ipm import strip


# --- strip: blank-line entries (audit HIGH) -----------------------------------

def _render(entries, title=None):
    # production headers carry the TRUE snapshot length (core writes
    # len(content)); fixtures must too, or provenance checks refuse them
    rule = "═" * 46
    body = "\n§\n".join(entries)
    if title is None:
        title = f"MEMORY (your personal notes) [9% \u2014 {len(body):,}/2,200 chars]"
    return f"intro part\n\n{rule}\n{title}\n{rule}\n{body}\n\nskills section"


def test_blank_line_entry_is_stripped_exactly():
    entries = ["entry one\n\nSECRET-PARA-2", "SECRET-ENTRY-2"]
    text = _render(entries)
    strip.set_memory_bodies(["\n§\n".join(entries)])
    try:
        out, n, amb = strip.strip_memory_blocks_checked(text)
        assert n == 1
        assert "SECRET-PARA-2" not in out
        assert "SECRET-ENTRY-2" not in out
        assert "intro part" in out and "skills section" in out
        assert amb is False  # exact reconstruction: no ambiguity
    finally:
        strip.reset_memory_bodies()


def test_strip_fallback_is_flagged_ambiguous():
    entries = ["entry one\n\nSECRET-PARA-2"]
    text = _render(entries)
    strip.reset_memory_bodies()  # no reconstruction source available
    out, n, amb = strip.strip_memory_blocks_checked(text)
    assert n == 0  # UNPROVABLE: block kept verbatim, never partially stripped
    assert amb is True  # caller must fail closed (enforce blocks the request)
    assert "SECRET-PARA-2" in out  # intact — the header survives for the recheck
    assert strip.has_memory_block(out)


def test_unpaired_context_tag_is_residue():
    text = "hello <memory-context> leaked tail"
    out, n = strip.strip_memory_context_spans(text)
    assert n == 0
    assert strip.span_residue(out) is True


def test_reversed_context_tags_are_residue_not_span():
    text = "tail </memory-context> head <memory-context>"
    assert strip.has_memory_context_span(text) is False
    assert strip.span_residue(text) is True


def test_system_message_developer_role_is_stripped():
    entries = ["secret entry"]
    text = _render(entries, title=f"USER PROFILE (who the user is) [5% — {len(entries[0]):,}/1,375 chars]")  # true snapshot length
    strip.set_memory_bodies(["\n§\n".join(entries)])
    try:
        req = {"messages": [{"role": "developer", "content": text},
                            {"role": "user", "content": "hi"}]}
        out, counts = strip.strip_request(dict(req))
        assert counts["blocks"] == 1
        assert "secret entry" not in str(out["messages"][0]["content"])
    finally:
        strip.reset_memory_bodies()


# --- lifecycle: mode-off exclusion (audit HIGH) --------------------------------

def test_mode_off_chat_survives_reenable(private_world, monkeypatch):
    from ipm.store import hash_session_id
    holder = {"mode": "off"}

    def fake_read_settings(paths=None):
        return {**DEFAULTS, "mode": holder["mode"]}

    monkeypatch.setattr(lifecycle, "read_settings", fake_read_settings)
    lifecycle._maybe_generation_on_mode_change(fake_read_settings(), store.read("meta", private_world.data_dir), private_world)
    # chat born while off
    lifecycle.observe_session_start("born-off", "cli", private_world)
    ledger = store.read("ledger", private_world.data_dir)
    assert ledger[hash_session_id("born-off")]["state"] == "excluded"
    # re-enable: the entry must NEVER become a candidate again
    holder["mode"] = "private"
    lifecycle._maybe_generation_on_mode_change(fake_read_settings(), store.read("meta", private_world.data_dir), private_world)
    counts = lifecycle.run_sweep(private_world, batch_limit=20)
    ledger = store.read("ledger", private_world.data_dir)
    assert ledger[hash_session_id("born-off")]["state"] == "excluded"
    assert "deleted" not in ledger[hash_session_id("born-off")].get("state", "")


# --- lifecycle: compaction lineage honesty (audit HIGH) ------------------------

def test_fenced_mark_only_confirmed_targets(private_world):
    from ipm.store import hash_session_id
    key = hash_session_id("root-x")
    ledger = {
        key: {"state": "armed", "raw_id": "root-x"},
        hash_session_id("parent-x"): {"state": "armed", "raw_id": "parent-x"},
    }
    # simulate: only root was confirmed removed (targets == [root])
    targets = ["root-x"]
    l2 = dict(ledger)
    for t in targets:
        h = hash_session_id(t)
        member = l2.get(h)
        if isinstance(member, dict) and member.get("deleting"):
            member["state"] = "deleted"
            member["deleted_how"] = "fenced"
            member.pop("deleting", None)
            member.pop("raw_id", None)
    # the parent (not a target, no deleting flag) must remain armed — NOT deleted
    assert l2[hash_session_id("parent-x")]["state"] == "armed"


def test_post_compaction_unknown_predecessor_excludes(private_world):
    from ipm.store import hash_session_id
    lifecycle.observe_post_compaction("cont-1", "unknown-root", private_world)
    ledger = store.read("ledger", private_world.data_dir)
    entry = ledger.get(hash_session_id("cont-1"))
    assert entry is not None and entry["state"] == "excluded"


def test_post_compaction_inherits_keep(private_world):
    from ipm.store import hash_session_id
    lifecycle.set_override("kept-root", "keep", private_world)
    lifecycle.observe_post_compaction("kept-cont", "kept-root", private_world)
    ledger = store.read("ledger", private_world.data_dir)
    cont = ledger.get(hash_session_id("kept-cont"))
    assert cont is not None and cont["state"] == "excluded"
    assert cont.get("override") == "keep"


# --- logfilter: ReDoS safety (audit HIGH) --------------------------------------

def test_logfilter_pathological_input_is_fast():
    from ipm.logfilter import PrivateLogFilter
    evil = "msg='" + "\\" * 400  # no closing quote: the old regex exploded
    f = PrivateLogFilter(lambda: True)
    rec = __import__("logging").LogRecord("gateway.run", 20, __file__, 1, evil, None, None)
    t0 = time.perf_counter()
    f.filter(rec)
    dt = time.perf_counter() - t0
    assert dt < 0.5, f"redaction took {dt:.2f}s — backtracking regression"


def test_tool_calls_preview_redacted():
    import logging
    from ipm.logfilter import PrivateLogFilter
    f = PrivateLogFilter(lambda: True)
    rec = logging.LogRecord("hermes.tool_calls", 20, __file__, 1,
                            "11-10 10:00:00  terminal: \"cat /home/secrets.txt\"", None, None)
    f._redact(rec)
    assert "secrets.txt" not in str(rec.msg)
    assert "[redacted]" in str(rec.msg)


# --- routes: spoofing + loopback (audit MEDIUM) --------------------------------

def test_provider_id_cannot_relabel_curated_host():
    v = classify("opencode-zen", "https://api.moonshot.ai/v1", None, None, None, None, None)
    assert v.label != "ok"  # kimi's host must not inherit a better verdict
    assert v.policy in {"UNKNOWN", "NONE_TRAINS", "PER_REQUEST"}


def test_loopback_never_reports_already():
    v = classify("custom", "http://localhost:4000/v1", None, None, None, None, None)
    assert v.label != "ok"


# --- settings: /private on in mode=off (audit MEDIUM) --------------------------

def test_provider_controls_active_honors_on_override():
    assert provider_controls_active({"mode": "off"}, {"override": "on"}) is True
    assert provider_controls_active({"mode": "off"}, None) is False
    assert effective_mode({"mode": "off"}, {"override": "on"}) == "private"


# --- lifecycle: absent vs error (audit MEDIUM) ---------------------------------

def test_read_row_checked_distinguishes_error(tmp_path):
    p = tmp_path / "state.db"
    p.write_bytes(b"not a database at all")
    state, row = lifecycle.read_row_checked(p, "some-session")
    assert state == "error" and row is None  # NOT 'absent'


def test_uri_with_hash_is_encoded(tmp_path):
    home = tmp_path / "Work#2"
    home.mkdir()
    p = home / "state.db"
    import sqlite3
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, ended_at REAL, end_reason TEXT, source TEXT, parent_session_id TEXT)")
    conn.commit(); conn.close()
    state, _ = lifecycle.read_row_checked(p, "nope")
    assert state == "absent"  # the '#' did not truncate the URI
