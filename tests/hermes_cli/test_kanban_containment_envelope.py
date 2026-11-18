"""Containment append envelopes and lifecycle phase recovery."""
from __future__ import annotations

import copy
import json

import pytest

from hermes_cli import kanban_containment_state as state
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as lifecycle
from tests.hermes_cli import test_kanban_containment_state as fixtures

board = lifecycle.board


@pytest.mark.parametrize("key", ["_recovery_history_v1", "_wake_episode_v1"])
def test_transport_envelope_is_not_a_new_predicate_or_release_authority(key):
    stop = fixtures.stop()
    receipt = fixtures.release(stop)
    stop["payload"][key] = {"fixture": ["causal-head"]}
    receipt["payload"][key] = {"fixture": ["other-head"]}
    assert state.reduce_events([receipt, stop]) is not None  # envelope grants nothing
    assert state.reduce_events([receipt, stop], verify_release=lambda *_: True) is None
    assert state.fingerprint(state._stop(stop)) == receipt["payload"]["containment_sha256"]


@pytest.mark.parametrize("key", ["_recovery_history_v1", "_wake_episode_v1"])
@pytest.mark.parametrize("value", [None, [], "forged", True])
def test_malformed_transport_metadata_is_unresolved_not_an_ignored_stop(key, value):
    stop = fixtures.stop()
    stop["payload"][key] = value
    hold = state.reduce_events([stop])
    assert hold is not None and hold.unresolved is True


def test_nullable_event_baseline_still_requires_a_real_portable_run():
    stop = fixtures.stop()
    stop["payload"]["policy"]["producer"] = fixtures.PRODUCER
    stop["payload"]["producer_baseline"] = {"run_uid": fixtures.STOP_UID, "event_uid": None}
    assert state.reduce_events([stop]).unresolved is False
    for field, value in (("run_uid", None), ("run_uid", 42), ("event_uid", 42)):
        broken = copy.deepcopy(stop)
        broken["payload"]["producer_baseline"][field] = value
        assert state.reduce_events([broken]).unresolved is True


@pytest.mark.parametrize("phase", ["contained", "containment_released"])
def test_old_review_resume_cannot_survive_a_containment_episode(board, phase):
    relay, _, authority, target, spec = lifecycle.setup(board)
    with kb.write_txn(board):
        kb._append_event(board, relay, "blocked", {"resume_status": "review"})
    assert kb._resume_status_from_events(board, relay) == "review"
    episode = lifecycle.contain(board, relay, target, spec)["episode"]
    assert kb._resume_status_from_events(board, relay) == "ready"
    stop = board.execute("SELECT * FROM task_events WHERE uid=?", (episode,)).fetchone()
    assert "_recovery_history_v1" in json.loads(stop["payload"])
    if phase == "containment_released":
        lifecycle.release(board, target, episode, authority)
        assert kb._resume_status_from_events(board, relay) == "ready"
    from hermes_cli.kanban_recovery_history import head
    assert head(board, relay)["kind"] == phase
