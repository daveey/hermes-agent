"""Corrupt constituent metadata must not discard opaque carrier bytes."""
import hashlib

import pytest

from agent.agent_runtime_helpers_user_rows import NOTICE_PARTS, _constituents


@pytest.mark.parametrize("offset_index,bad_offset", [
    (0, False), (1, True), (2, False), (3, True),
    (0, -1), (2, -1), (1, 2), (3, 2),
], ids=["display-bool-start", "display-bool-end", "model-bool-start", "model-bool-end",
        "display-negative-start", "model-negative-start", "display-overrun-end", "model-overrun-end"])
def test_invalid_offsets_preserve_opaque_identity(offset_index, bad_offset):
    # Python accepts bools, translates negative indices and clamps overrun bounds.
    # Each malformed slice still reconstructs the entire row, isolating the guard.
    display = model = "A"
    offsets = [0, 1, 0, 1]
    offsets[offset_index] = bad_offset
    row = {
        "role": "user", "display_kind": "kanban_notice",
        "content": display, "api_content": model,
        "display_metadata": {
            "notice_id": "A",
            NOTICE_PARTS: [{
                "notice_id": "A", "offsets": offsets,
                "display_sha256": hashlib.sha256(display.encode()).hexdigest(),
            }],
        },
    }
    assert _constituents(row) == [{
        "content": display, "api_content": model, "notice_id": None,
    }]


@pytest.mark.parametrize("omitted_field", ["content", "api_content"])
def test_incomplete_spans_preserve_opaque_carrier(omitted_field):
    display = "notice A\n\nunrelated trailing request"
    model = display + "\n[ask-gate] live state A"
    display_end = len("notice A") if omitted_field == "content" else len(display)
    model_end = len(display) if omitted_field == "api_content" else len(model)
    row = {
        "role": "user", "display_kind": "kanban_notice",
        "content": display, "api_content": model,
        "display_metadata": {
            "notice_id": "A",
            NOTICE_PARTS: [{
                "notice_id": "A", "offsets": [0, display_end, 0, model_end],
                "display_sha256": hashlib.sha256(display[:display_end].encode()).hexdigest(),
            }],
        },
    }
    assert _constituents(row) == [{
        "content": display, "api_content": model, "notice_id": None,
    }]
