"""Opaque notice repair preserves model bytes without exposing them to display."""
import hashlib
import json

import pytest

from agent.agent_runtime_helpers import repair_message_sequence
from agent.agent_runtime_helpers_user_rows import NOTICE_PARTS


def notice():
    return {"role": "user", "content": "notice A", "display_kind": "kanban_notice",
            "display_metadata": {"notice_id": "A"}}


@pytest.mark.parametrize("omitted", ["display_suffix", "model_suffix"])
@pytest.mark.parametrize("opaque_first", [False, True])
def test_public_repair_preserves_opaque_sidecar(omitted, opaque_first):
    display = "notice A\n\nunrelated trailing request"
    model = display + "\n[ask-gate] live state A"
    b = len("notice A") if omitted == "display_suffix" else len(display)
    d = len(model) if omitted == "display_suffix" else len(display)
    opaque = {**notice(), "content": display, "api_content": model,
              "display_metadata": {"notice_id": "A", NOTICE_PARTS: [{
                  "notice_id": "A", "offsets": [0, b, 0, d],
                  "display_sha256": hashlib.sha256(display[:b].encode()).hexdigest()}]}}
    pair = [opaque, notice()] if opaque_first else [notice(), opaque]
    rows = [{"role": "assistant", "content": "ok"}, *pair]
    repair_message_sequence(None, rows)
    kept = rows[-1]
    assert kept["content"] == (display + "\n\nnotice A" if opaque_first else "notice A\n\n" + display)
    assert kept["api_content"] == (model + "\n\nnotice A" if opaque_first else "notice A\n\n" + model)
    assert [p["notice_id"] for p in kept["display_metadata"][NOTICE_PARTS]] == (
        [None, "A"] if opaque_first else ["A", None])
    assert "[ask-gate]" not in kept["content"]
    assert "[ask-gate]" not in json.dumps(kept["display_metadata"])
    assert repair_message_sequence(None, rows) == 0


@pytest.mark.parametrize("sidecar", [None, 42, {}, ["model"]])
def test_nonstring_opaque_sidecar_uses_display(sidecar):
    opaque = {**notice(), "content": "opaque request", "api_content": sidecar,
              "display_metadata": {NOTICE_PARTS: [None]}}
    rows = [{"role": "assistant", "content": "ok"}, opaque, notice()]
    repair_message_sequence(None, rows)
    assert rows[-1]["content"] == "opaque request\n\nnotice A"
    assert rows[-1]["api_content"] == rows[-1]["content"]


@pytest.mark.parametrize("with_notice", [False, True])
def test_ordinary_row_sidecar_contract(with_notice):
    rows = [{"role": "assistant", "content": "ok"},
            {"role": "user", "content": "request", "api_content": "request\nmodel-only"},
            notice() if with_notice else {"role": "user", "content": "next"}]
    repair_message_sequence(None, rows)
    assert rows[-1]["content"] == ("request\n\nnotice A" if with_notice else "request\n\nnext")
    if with_notice:
        assert rows[-1]["api_content"] == "request\nmodel-only\n\nnotice A"
        assert "model-only" not in json.dumps(rows[-1]["display_metadata"])
    else:
        assert "api_content" not in rows[-1]
