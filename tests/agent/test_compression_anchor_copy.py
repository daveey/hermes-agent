"""Restored intent anchors must not change the source transcript's durability."""

from copy import deepcopy

from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.conversation_compression import _ensure_compressed_has_user_turn


def test_anchor_copy_does_not_stamp_the_original_request():
    request = {"role": "user", "content": "please do X"}
    original = [{"role": "system", "content": "s"}, request]
    before = deepcopy(original)
    tool_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ],
    }
    compressed = [tool_call]

    outcome = _ensure_compressed_has_user_turn(original, compressed)

    assert outcome == "inserted"
    assert compressed == [
        {"role": "user", "content": "please do X", _DB_PERSISTED_MARKER: True},
        tool_call,
    ]
    # Stamping the restored anchor must not mark the source request as persisted.
    assert original == before
    assert request == {"role": "user", "content": "please do X"}


def test_existing_request_needs_no_anchor_or_persistence_stamp():
    request = {"role": "user", "content": "please do X"}
    original = [{"role": "system", "content": "s"}, request]
    compressed = [request]
    before = deepcopy(original)

    assert _ensure_compressed_has_user_turn(original, compressed) == "already_present"

    assert compressed == [before[-1]]
    assert original == before
