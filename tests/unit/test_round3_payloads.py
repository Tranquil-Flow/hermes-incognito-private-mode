"""Round-3 boundary regressions. Real core rendering; no provider calls."""
import copy
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ipm import enforce, errors, logfilter, store, strip
from ipm.settings import DEFAULTS


@pytest.fixture
def world(private_world, monkeypatch):
    settings = {**DEFAULTS, "mode": "private", "allow_unverified": True,
                "use_memory": False, "strip_persona": True}
    monkeypatch.setattr(enforce, "capture_paths", lambda: private_world)
    monkeypatch.setattr(enforce, "read_settings", lambda paths=None: settings)
    return private_world, settings


def render(body):
    from tools.memory_tool_store import MemoryStore
    return MemoryStore()._render_block("memory", [body])


def request(system):
    return {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": "hello"}], "model": "test"}


def send(payload, sid="round3", **extra):
    seen = []
    result = enforce.on_llm_execution(
        request=payload, session_id=sid, platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions",
        next_call=lambda req: seen.append(req) or "sent", **extra)
    return result, seen


def test_execution_blocks_unstripped_real_core_snapshot(world):
    """The final boundary is independent of the request hook: memory that
    survived to execution (§3.7: marker present + use_memory=false) BLOCKS —
    it must never reach next_call, whatever the request phase did."""
    payload = request(render("old entry\n\nSNAPSHOT-TAIL"))
    result, seen = send(payload)
    assert result != "sent" and seen == []
    assert "SNAPSHOT-TAIL" in str(payload)  # caller untouched


@pytest.mark.parametrize("entries", [
    ["first\n\nPRIVATE-TAIL", "second"],
    ["café\nprivate naïve words", "café\nprivate naïve words"],
])
def test_frozen_snapshot_after_disk_save_with_core_loader(world, monkeypatch, entries):
    from tools.memory_tool_store import MemoryStore
    import hermes_constants
    paths, _settings = world
    paths.memory_path.parent.mkdir(parents=True, exist_ok=True)
    paths.memory_path.write_text("\ufeff" + "\n§\n".join(entries))
    monkeypatch.setattr(MemoryStore, "_path_for", lambda self, target:
                        paths.memory_path if target == "memory" else paths.user_profile_path)
    monkeypatch.setattr(hermes_constants, "mkdir_under_hermes_home", lambda path: path.mkdir(parents=True, exist_ok=True))
    memory = MemoryStore()
    memory.load_from_disk()
    snapshot = memory.format_for_system_prompt("memory")
    assert snapshot
    paths.memory_path.write_text("newly saved, different text")
    payload = request(snapshot + "\n\nKEEP-NEIGHBOUR")
    # The file has diverged, so the snapshot body cannot be proven against fed
    # candidates: the request phase leaves the block INTACT and returns None.
    transformed = enforce.on_llm_request(
        request=payload, session_id="save", platform="cli", provider="custom",
        base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions")
    assert transformed is None
    result, seen = send(payload, "save")
    assert result != "sent" and seen == []  # execution BLOCKS (fail closed)
    assert entries[0] in payload["messages"][0]["content"]  # intact, never partial


def test_ambiguous_block_keeps_header_for_independent_recheck(world):
    rule = "═" * 46
    malformed = f"{rule}\nMEMORY (your personal notes) [41%]\n{rule}\nfirst\n\nPRIVATE-RESIDUE"
    out, _counts = strip.strip_request(request(malformed), bodies=[])
    assert strip.has_memory_block(out["messages"][0]["content"])
    result, seen = send(out)
    assert result != "sent" and seen == []


def test_concurrent_memory_passes_never_use_legacy_global(world, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("shared bodies API must not be used")
    monkeypatch.setattr(strip, "set_memory_bodies", forbidden)
    monkeypatch.setattr(strip, "reset_memory_bodies", forbidden)
    paths, _settings = world
    barrier = threading.Barrier(6)
    def worker(n):
        paths.memory_path.parent.mkdir(parents=True, exist_ok=True)
        barrier.wait(timeout=5)
        for i in range(60):
            body = f"snapshot-{n}-{i}\n\nPRIVATE-TAIL"
            # production wiring: the current file feeds the strip candidates.
            # Six workers share one profile file, so a raced read may pair a
            # foreign body — the ONLY allowed outcomes are: matched & stripped
            # (sent clean) or unprovable & BLOCKED. Never a leak.
            paths.memory_path.write_text(body)
            payload = request(render(body))
            transformed = enforce.on_llm_request(request=payload, session_id=f"t{n}", platform="cli",
                provider="custom", base_url="http://127.0.0.1:8391/v1", api_mode="chat_completions")
            result, seen = send(transformed["request"] if transformed else payload, f"t{n}")
            if transformed is not None:
                assert result == "sent" and "PRIVATE-TAIL" not in str(seen[0])
            else:
                assert result != "sent" and seen == []
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))


def test_stale_persona_flag_never_skips_final_strip(world, monkeypatch):
    paths, _settings = world
    persona = "# Café persona\nAlways keep this private. " * 4
    (paths.home / "SOUL.md").write_text(persona)
    enforce._strip_state.persona_stripped = 7
    enforce._strip_state.ambiguous = True
    monkeypatch.setattr(enforce, "capture_paths", lambda: (_ for _ in ()).throw(RuntimeError("probe")))
    assert enforce.on_llm_request(request=request(persona)) is None
    monkeypatch.setattr(enforce, "capture_paths", lambda: paths)
    result, seen = send(request(persona))
    assert result == "sent" and persona.strip() not in str(seen[0])


def test_persona_normalized_presence_is_not_absent(world):
    paths, _settings = world
    persona = "# Café persona\nKeep this thoughtful and private.\n" * 4
    (paths.home / "SOUL.md").write_text(persona)
    rendered = persona.replace("\n", "\n\n")  # loader changed whitespace; exact replacement misses
    result, seen = send(request(rendered))
    assert result == "sent" and "Café persona" in str(seen[0])
    record = json.loads((paths.data_dir / "audit.jsonl").read_text().splitlines()[-1])
    assert record["persona"] == "present-not-located"


def test_persona_only_rewrites_system_surfaces(world):
    persona = "You are an entirely private personal assistant. " * 3
    payload = request(persona)
    for role in ("user", "assistant", "tool"):
        payload["messages"].append({"role": role, "content": [{"type": "text", "text": persona}]})
    original = copy.deepcopy(payload)
    out, count = strip.replace_persona_span(payload, persona.strip())
    assert count == 1
    assert out["messages"][1:] == original["messages"][1:]
    assert payload == original


def test_policy_error_is_specific():
    nous = {"error_message": "No endpoint for MODEL meets this request's data-use requirement",
            "error_body": '{"error":{"code":"data_policy_denied"}}'}
    assert errors.classify_error(nous, mode_active=True, provider="nous")["retryable"] is False
    for text in ("No endpoints found for model X", "No endpoints found that support tool use", "billing data policy documentation"):
        assert errors.classify_error({"error_message": text}, mode_active=True, provider="openrouter") is None


def test_logfilter_settings_failure_is_not_content_passthrough(monkeypatch):
    def broken():
        raise RuntimeError("no settings")
    record = logging.LogRecord("hermes.tool_calls", 20, "x", 1, "SECRET-PREVIEW", (), None)
    filt = logfilter.PrivateLogFilter(broken)
    assert filt.filter(record)
    assert "SECRET-PREVIEW" not in record.getMessage()


def test_logfilter_render_error_is_content_free():
    record = logging.LogRecord("hermes.tool_calls", 20, "x", 1, "SECRET %s %s", ("one",), None)
    filt = logfilter.PrivateLogFilter(lambda: True)
    assert filt.filter(record)
    assert "SECRET" not in str(record.msg)
    assert not record.args


def test_audit_append_really_waits_for_store_lock(world, monkeypatch):
    paths, _settings = world
    attempted = threading.Event()
    entered = threading.Event()
    real_lock = store.store_lock
    from contextlib import contextmanager
    @contextmanager
    def watched_lock(*args, **kwargs):
        attempted.set()
        with real_lock(*args, **kwargs) as acquired:
            entered.set()
            yield acquired
    monkeypatch.setattr(store, "store_lock", watched_lock)
    thread = threading.Thread(target=enforce._audit_append, args=(paths, {"label": "probe"}))
    with real_lock(paths.data_dir):
        thread.start()
        assert attempted.wait(timeout=3)
        assert not entered.is_set()
        assert not (paths.data_dir / "audit.jsonl").exists()
    thread.join(timeout=5)
    assert not thread.is_alive() and entered.is_set()
    assert json.loads((paths.data_dir / "audit.jsonl").read_text())["label"] == "probe"
