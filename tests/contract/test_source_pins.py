"""Contract tests pinning source formats (BUILD_SPEC §4, §6 row 3).

Pins: memory block markers, <memory-context>, Claude Code prefix, log formats,
end_reason allowlist + automatic set, delete fence signature/behaviour, writer
file naming, store:false in the codex transport, config_schema keys.
Private core imports allowed in test files only (§4).
"""

import inspect
import re
from pathlib import Path

CORE = Path("/Users/evinova-self/.hermes/hermes-agent")
PLUGIN = Path(__file__).resolve().parents[2]


# --- 1. memory block markers (tools/memory_tool_store.py:22, _render_block) ----

def test_memory_block_headers_pinned():
    from tools.memory_tool_store import ENTRY_DELIMITER, MEMORY_BLOCK_HEADERS

    assert MEMORY_BLOCK_HEADERS["memory"] == "MEMORY (your personal notes)"
    assert MEMORY_BLOCK_HEADERS["user"] == "USER PROFILE (who the user is)"
    assert ENTRY_DELIMITER == "\n§\n"
    from ipm.strip import MEMORY_TITLE_RE

    for title in MEMORY_BLOCK_HEADERS.values():
        assert MEMORY_TITLE_RE.match(f"{title} [13%· 2197/2200]")
        assert MEMORY_TITLE_RE.match(f"{title} [9%]")


def test_render_block_format_matches_strip():
    from tools.memory_tool_store import ENTRY_DELIMITER, MEMORY_BLOCK_HEADERS

    source = inspect.getsource(
        __import__("tools.memory_tool_store", fromlist=["_render_block"]).__dict__["_nope_"]
    ) if False else None
    # Replicate _render_block's documented format (read from source):
    src = (CORE / "tools" / "memory_tool_store.py").read_text()
    assert '"═" * 46' in src or '═' * 46 in src
    sep = "═" * 46
    block = f"{sep}\n{MEMORY_BLOCK_HEADERS['memory']} [10%]\n{sep}\nentry-a{ENTRY_DELIMITER}entry-b"
    from ipm.strip import strip_memory_blocks

    out, n = strip_memory_blocks(f"before\n\n{block}\n\nafter")
    assert n == 1
    assert "entry-a" not in out and "entry-b" not in out
    assert "before" in out and "after" in out


# --- 2. <memory-context> spans (agent/memory_context.py) -----------------------

def test_memory_context_tags_pinned():
    from agent.memory_context import StreamingContextScrubber

    assert StreamingContextScrubber._OPEN_TAG == "<memory-context>"
    assert StreamingContextScrubber._CLOSE_TAG == "</memory-context>"
    from ipm.strip import MEMORY_CONTEXT_OPEN, MEMORY_CONTEXT_CLOSE

    assert (MEMORY_CONTEXT_OPEN, MEMORY_CONTEXT_CLOSE) == ("<memory-context>", "</memory-context>")


# --- 3. Anthropic subscription marker (agent/anthropic_adapter.py:318) ---------

def test_claude_code_prefix_pinned():
    from agent.anthropic_adapter import _CLAUDE_CODE_SYSTEM_PREFIX

    assert _CLAUDE_CODE_SYSTEM_PREFIX == "You are Claude Code, Anthropic's official CLI for Claude."


# --- 4. log formats (gateway/run_turn.py:215-227, agent/turn_context.py:1094-1100)

def test_gateway_inbound_format_pinned():
    src = (CORE / "gateway" / "run_turn.py").read_text()
    assert "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r" in src


def test_turn_context_format_pinned():
    src = (CORE / "agent" / "turn_context.py").read_text()
    assert "conversation turn: session=%s model=%s provider=%s platform=%s history=%d msg=%r" in src


# --- 5. end_reason allowlist + automatic set (pinned against sources) ----------

def test_automatic_end_reasons_pinned():
    from tui_gateway.session_lifecycle import _AUTOMATIC_SESSION_END_REASONS
    from ipm.lifecycle import AUTOMATIC_END_REASONS

    assert _AUTOMATIC_SESSION_END_REASONS == AUTOMATIC_END_REASONS


def test_allowlist_reasons_have_source_sites():
    from ipm.lifecycle import ALLOWED_END_REASONS

    sites = {
        "cli_close": (CORE / "hermes_cli" / "cli_shutdown.py"),
        "new_session": (CORE / "hermes_cli" / "cli_session_mixin.py"),
        "resumed_other": (CORE / "hermes_cli" / "cli_commands_mixin.py"),
        "branched": (CORE / "hermes_cli" / "cli_commands_mixin.py"),
        "tui_close": (CORE / "tui_gateway" / "session_lifecycle.py"),
        "agent_close": (CORE / "run_agent.py"),
    }
    for reason, path in sites.items():
        assert reason in ALLOWED_END_REASONS
        assert reason in path.read_text(), f"{reason} no longer written by {path.name}"


def test_no_unknown_explicit_end_reason_literals():
    """Every literal end reason written by core end_session sites must be either in
    our allowlist, in the automatic set, or on this reviewed ignore list."""
    from ipm.lifecycle import ALLOWED_END_REASONS, AUTOMATIC_END_REASONS

    ignore = {"compression", "finalized", "expired", "reason", "end_reason",
              "invalidation_reason", "maintenance",
              "adopted_by_profile"}  # profile adoption re-homes rows; not a user close
    literals = set()
    for py in CORE.glob("*.py"):
        literals |= set(re.findall(r"end_session\([^)]*?['\"]([a-z_]+)['\"]", py.read_text(errors="ignore")))
    for py in (CORE / "hermes_cli").glob("*.py"):
        literals |= set(re.findall(r"end_session\([^)]*?['\"]([a-z_]+)['\"]", py.read_text(errors="ignore")))
    for py in (CORE / "tui_gateway").glob("*.py"):
        literals |= set(re.findall(r"end_session\([^)]*?['\"]([a-z_]+)['\"]", py.read_text(errors="ignore")))
    unknown = literals - ALLOWED_END_REASONS - AUTOMATIC_END_REASONS - ignore
    assert not unknown, f"new core end reasons need review: {sorted(unknown)}"


# --- 6. delete fence signature/behaviour (hermes_state_sessions.py:1668-1780) --

def test_fenced_delete_signature_pinned():
    import hermes_state

    params = inspect.signature(hermes_state.SessionDB.delete_session).parameters
    for name in ("session_id", "sessions_dir", "expected_delete_ids",
                 "exclude_active_write_guards", "include_compression_chain"):
        assert name in params
    assert hasattr(hermes_state.SessionDB, "get_session_delete_targets")


def test_plugin_delete_uses_the_fence_only():
    import ast

    tree = ast.parse((PLUGIN / "ipm" / "lifecycle.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    delete_calls = [c for c in calls
                    if getattr(c.func, "attr", None) == "delete_session"]
    assert len(delete_calls) == 1, "delete_session must be called exactly once"
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in delete_calls[0].keywords}
    assert kwargs.get("sessions_dir") == "None"
    assert kwargs.get("expected_delete_ids") == "targets"
    assert kwargs.get("exclude_active_write_guards") == "True"
    assert "include_compression_chain" not in kwargs  # never (§3.1)
    # No subprocesses anywhere in the plugin: the in-process public API only.
    for py in list((PLUGIN / "ipm").glob("*.py")) + [PLUGIN / "__init__.py"]:
        body = py.read_text()
        assert "subprocess" not in body, py.name
        assert "os.system" not in body, py.name


def test_retirement_fence_present():
    src = (CORE / "hermes_state_mutation_retirement.py").read_text()
    assert "retire_sessions" in src  # public delete writes retirement fences (§3.1)


# --- 7. writer file naming (agent_runtime_helpers.py:1585, session_persistence:63)

def test_session_filename_component_matches_core():
    from agent.session_persistence import _safe_session_filename_component
    from ipm.lifecycle import session_filename_component

    corpus = ["abc", "abc-extra", "abc_extra", "sess.1", "local-abc_1", "x:y",
              "a" * 250, "plain-id_42", "dots.and:colons", "trail-", ".lead"]
    for sid in corpus:
        assert session_filename_component(sid) == _safe_session_filename_component(sid), sid


def test_dump_name_matcher_never_matches_neighbour():
    """abc's dump cleanup must not match abc_extra's dumps (§6 row 17)."""
    import os
    from conftest import FakePaths
    from ipm.lifecycle import cleanup_session_files

    paths = FakePaths(Path(os.environ["HOME"]).parent / "dump-neighbour")
    paths.sessions_dir.mkdir(parents=True, exist_ok=True)
    dumps = {
        "request_dump_abc_20261110_120000_000001.json": "abc's dump",
        "request_dump_abc_extra_20261110_120000_000002.json": "abc_extra's dump",
        "request_dump_abc_extra_20261110_120000_999999.json": "abc_extra's dump 2",
        "request_dump_abcSUS_20261110_120000_000003.json": "another id's dump",
    }
    for name in dumps:
        (paths.sessions_dir / name).write_text(dumps[name])
    removed = cleanup_session_files(paths, ["abc"])
    assert removed == 1
    assert (paths.sessions_dir / "request_dump_abc_20261110_120000_000001.json").exists() is False
    for name, content in dumps.items():
        if name != "request_dump_abc_20261110_120000_000001.json":
            assert (paths.sessions_dir / name).read_text() == content


def test_exact_snapshot_names():
    import os
    from conftest import FakePaths
    from ipm.lifecycle import cleanup_session_files

    paths = FakePaths(Path(os.environ["HOME"]).parent / "dump-names")
    paths.sessions_dir.mkdir(parents=True, exist_ok=True)
    for name in ["abc.json", "abc.jsonl", "session_abc.json", "keep.json"]:
        (paths.sessions_dir / name).write_text("x")
    removed = cleanup_session_files(paths, ["abc"])
    assert removed == 3
    assert (paths.sessions_dir / "keep.json").exists()


# --- 8. store:false already set by the codex transport (§3.7) -------------------

def test_codex_transport_store_false_pinned():
    src = (CORE / "agent" / "transports" / "codex.py").read_text()
    assert '"store": False' in src


# --- 9. config_schema keys == settings DEFAULTS ---------------------------------

def test_config_schema_keys_match_defaults():
    import yaml

    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    from ipm.settings import DEFAULTS

    schema_keys = set(manifest["config_schema"].keys())
    assert schema_keys == set(DEFAULTS.keys())


# --- 10. capabilities == registered set -----------------------------------------

def test_capabilities_equal_registrations():
    import yaml

    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    src = (PLUGIN / "__init__.py").read_text()
    for hook in manifest["provides_hooks"]:
        assert f'"{hook}"' in src, hook
    for mw in manifest["provides_middleware"]:
        assert f'"{mw}"' in src, mw


# --- 11. sanitize/validate: session ids regex matches spec §4 -------------------

def test_session_id_regex_spec():
    from ipm.store import validate_session_id

    assert validate_session_id("A1b2C3.d:e_f-g")
    assert not validate_session_id("x/y")
    assert not validate_session_id("x y")
