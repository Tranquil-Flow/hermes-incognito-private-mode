"""Unit: transforms copy-on-write ×3 shapes + memory strip ×3 + memory-context (§6 row 2)."""

import copy

import pytest

from ipm import strip, transforms


@pytest.fixture(autouse=True)
def _memory_sources():
    """Block fixtures mirror production: enforce feeds the current file bodies
    (title-skip removed — files are entries-only)."""
    strip.set_memory_bodies([
        "User: literal docs/gaps; team Qs last.\n\u00a7\nAstra: ~/Projects/Astra-Hermes/.",
        "Evi (she/her)/Tranquil-Flow.",
    ])
    try:
        yield
    finally:
        strip.reset_memory_bodies()

RULE = "═" * 46
BLOCK = (
    f"{RULE}\nMEMORY (your personal notes) [13%· 2197/2200]\n{RULE}\n"
    "User: literal docs/gaps; team Qs last.\n§\nAstra: ~/Projects/Astra-Hermes/."
)
USER_BLOCK = f"{RULE}\nUSER PROFILE (who the user is) [9%]\n{RULE}\nEvi (she/her)/Tranquil-Flow."
PLAIN_SYSTEM = "You are a helpful assistant with tools."


def test_strip_removes_blocks_in_chat_completions():
    system = f"{PLAIN_SYSTEM}\n\n{BLOCK}\n\n{USER_BLOCK}\n\nRemain close."
    request = {"messages": [{"role": "system", "content": system},
                            {"role": "user", "content": "hello"}]}
    new_request, counts = strip.strip_request(request)
    assert counts["blocks"] == 2
    assert counts["spans"] == 0
    out = new_request["messages"][0]["content"]
    assert "MEMORY (your personal notes)" not in out
    assert "USER PROFILE (who the user is)" not in out
    assert "literal docs/gaps" not in out
    assert "Evi (she/her)" not in out
    assert PLAIN_SYSTEM in out
    assert "Remain close." in out
    # untouched request object (copy-on-write)
    assert "MEMORY (your personal notes)" in request["messages"][0]["content"]


def test_strip_codex_instructions():
    request = {"instructions": f"{BLOCK}", "input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": f"before <memory-context>secret recall</memory-context> after"}]}]}
    new_request, counts = strip.strip_request(request)
    assert counts["blocks"] == 1
    assert counts["spans"] == 1
    assert "MEMORY (your personal notes)" not in new_request["instructions"]
    text = new_request["input"][0]["content"][0]["text"]
    assert "secret recall" not in text
    assert "before" in text and "after" in text


def test_strip_anthropic_system_blocks_and_list():
    request = {"system": f"{BLOCK}", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "q <memory-context>z</memory-context>"}]}]}
    new_request, counts = strip.strip_request(request)
    assert counts["blocks"] == 1
    assert counts["spans"] == 1
    assert "MEMORY" not in new_request["system"]
    assert "z</memory" not in str(new_request["messages"])


def test_strip_noop_returns_same_object():
    request = {"messages": [{"role": "system", "content": PLAIN_SYSTEM},
                            {"role": "user", "content": "plain"}]}
    new_request, counts = strip.strip_request(request)
    assert new_request is request  # copy-on-write: nothing removed -> nothing copied
    assert counts == {"blocks": 0, "spans": 0, "ambiguous": 0}


def test_strip_does_not_eat_neighbouring_blocks():
    system = f"{BLOCK}\n\n{PLAIN_SYSTEM}"
    new_request, counts = strip.strip_request({"messages": [{"role": "system", "content": system}]})
    assert counts["blocks"] == 1
    assert PLAIN_SYSTEM in new_request["messages"][0]["content"]


def test_transforms_provider_routing_copy_on_write():
    request = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    original = copy.deepcopy(request)
    new_request, trace = transforms.apply_provider_routing(request)
    assert request == original  # caller's dict untouched
    assert transforms.flags_present(new_request) is True
    provider = new_request["extra_body"]["provider"]
    assert provider["data_collection"] == "deny"
    assert provider["zdr"] is True
    assert trace["ok"] is True


def test_transforms_preserves_only_order_sort():
    request = {"extra_body": {"provider": {"only": ["DeepSeek"], "order": ["DeepSeek"], "sort": "throughput"}}}
    new_request, _ = transforms.apply_provider_routing(request)
    provider = new_request["extra_body"]["provider"]
    assert provider["only"] == ["DeepSeek"]
    assert provider["order"] == ["DeepSeek"]
    assert provider["sort"] == "throughput"


def test_transforms_overrides_explicit_allow_with_trace():
    request = {"extra_body": {"provider": {"data_collection": "allow"}}}
    new_request, trace = transforms.apply_provider_routing(request)
    assert new_request["extra_body"]["provider"]["data_collection"] == "deny"
    assert trace["overridden"] and "allow" in trace["overridden"][0]


def test_flags_present_and_store_false_verify():
    assert transforms.flags_present({"extra_body": {"provider": {"data_collection": "deny", "zdr": True}}}) is True
    assert transforms.flags_present({"extra_body": {"provider": {"data_collection": "deny"}}}) is False
    assert transforms.verify_store_false({"store": False}) is True
    assert transforms.verify_store_false({"store": True}) is False
    assert transforms.verify_store_false({}) is False


def test_memory_markers_present_detection():
    request = {"messages": [{"role": "system", "content": f"{BLOCK}"}]}
    assert transforms.memory_markers_present(request) is True
    clean = {"messages": [{"role": "system", "content": PLAIN_SYSTEM}]}
    assert transforms.memory_markers_present(clean) is False
