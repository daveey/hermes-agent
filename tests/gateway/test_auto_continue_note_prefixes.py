"""[daveey] The gateway's auto-continue note prefixes stay pinned to the notes Desktop actually writes.

A core-update pause in a gateway-owned chat is forwarded to the gateway's FIFO with
``tui_gateway.update_pause.pause_note(...)`` (t_01a1d4920ce3cbf4). The gateway keeps its own literal prefixes
(``gateway.run`` never imports tui_gateway at load), so a reworded note would otherwise be replayed as a real user
message. These tests run the gateway's stripper on real ``pause_note`` output instead of a copied literal.
"""
from __future__ import annotations

import pytest

from gateway.run import (_AUTO_CONTINUE_NOTE_PREFIX, _PAUSE_REPLAY_NOTE_PREFIX, _is_auto_continue_noise,
                         _strip_auto_continue_noise)
from tui_gateway import session_history
from tui_gateway import update_pause as up

PROMPT = "[Kanban notice] t_01a1d3efb167bf46 done"


def test_gateway_prefixes_open_the_tui_notes():
    assert up.PAUSE_REPLAY_NOTE_PREFIX.startswith(_PAUSE_REPLAY_NOTE_PREFIX)
    assert session_history._AUTO_CONTINUE_NOTE_PREFIX.startswith(_AUTO_CONTINUE_NOTE_PREFIX)
    assert session_history._PAUSE_REPLAY_NOTE_PREFIX is up.PAUSE_REPLAY_NOTE_PREFIX  # one definition


@pytest.fixture()
def clock(monkeypatch):
    monkeypatch.setattr(up, "_now", lambda: 10_000.0)
    return 10_000.0


@pytest.mark.parametrize("variant", ["plain", "later_turns", "queued", "chain", "unknown_old"])
def test_gateway_strips_every_pause_note_down_to_the_request(clock, variant):
    kwargs = {"plain": {},
              "later_turns": {"paused_at": clock - 600, "later_turns": 2},
              "queued": {"paused_at": clock - 30, "queued": True},
              "chain": {"paused_at": clock - 3600, "queued": True, "pauses": 3, "later_turns": 1},
              "unknown_old": {"paused_at": clock - 2 * 3600, "later_turns": None}}[variant]
    note = up.pause_note(PROMPT, **kwargs)
    if variant == "plain":
        assert note.startswith(session_history._AUTO_CONTINUE_NOTE_PREFIX)
    else:
        assert note.startswith(up.PAUSE_REPLAY_NOTE_PREFIX)
    if variant == "chain":
        assert "This request is not new" in note
    assert _is_auto_continue_noise(note)
    assert _strip_auto_continue_noise(note) == PROMPT
    assert session_history._legacy_display_kind("user", note) == "auto_continue"


def test_stripper_keeps_trailing_job_report_and_carried_note(clock):
    jobs = [{"session_id": "proc_1", "command": "sleep 1", "started_at": clock - 60}]
    note = up.pause_note(PROMPT, jobs, carried=True, paused_at=clock - 600, later_turns=1)
    stripped = _strip_auto_continue_noise(note)
    assert stripped.startswith(PROMPT) and stripped.endswith(up.CARRIED_TURN_NOTE)
