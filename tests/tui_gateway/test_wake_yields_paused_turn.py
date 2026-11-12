"""A headless wake leaves a turn paused for an update to the restarting backend. [daveey]

2026-11-11 04:10 UTC, chat 20261111_033213_db5894: the Desktop-facing backend paused the chat's turn for a Hermes
update (withdrawing its open clarify form) and restarted. 21 s after the pause, before the new backend's
continuation had built its agent, the kanban sweeper of the messaging gateway process woke the chat for a waiting
notice. That cold ``session.resume`` scheduled the paused turn's continuation in the gateway process, which no
Desktop window is attached to: the continuation re-asked the form there, the Desktop showed nothing, and the user's
next message took the chat over and cancelled it ("the response disappeared").

``server._wake_session_turn`` now answers "busy" without resuming while the chat carries a fresh pause whose writer
is gone, so the successor's startup sweep continues the turn where its viewers are.
"""
import json
import time
import types

import pytest

from tests.tui_gateway import test_update_pause_recovery as _recovery
from tui_gateway import server, update_pause as up
from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker, record_turn_start

recovery = _recovery.recovery  # the real session.resume wiring over a dead writer's durable pause
assert_continued = _recovery.assert_continued

KEY = "paused"
_REAL_PROFILE_HOME = server._profile_home  # the recovery fixture stubs it; these tests need the real resolver


@pytest.fixture(autouse=True)
def _waker_predates_the_pause(monkeypatch):
    """This process did not restart with the writer: the window counts from the pause stamp (the other clock is
    covered in test_cold_resume_yields_paused_turn.py)."""
    monkeypatch.setattr(up, "_process_started_at", lambda: 0.0)


def _loaded() -> bool:
    return server._find_live_session_by_key(KEY) is not None


def _edit_marker(home, **fields) -> None:
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries[KEY].update(fields)
    path.write_text(json.dumps(entries))


def test_headless_wake_leaves_a_fresh_paused_turn_to_its_successor(recovery, caplog):
    before = read_turn_marker(recovery.home, KEY)
    assert before["reason"] == up.PAUSE_REASON
    with caplog.at_level("INFO", logger="tui_gateway.update_pause"):
        verdict = server._wake_session_turn(KEY, "a card finished", display_kind="kanban_notice",
                                            label="kanban-sweep-wake")
    assert verdict == "busy"  # the sweeper writes the passive notice row; the continuation reads it
    assert not recovery.done.wait(0.5) and recovery.calls == [], "the wake took the paused turn's continuation"
    assert not _loaded(), "the wake resumed the paused chat in a process without its viewers"
    assert read_turn_marker(recovery.home, KEY) == before
    assert "paused for a Hermes update" in caplog.text


def test_wake_continues_a_pause_no_successor_picked_up(recovery):
    old = time.time() - up.SUCCESSOR_YIELD_UNKNOWN_S - 1
    _edit_marker(recovery.home, started_at=old, paused_at=old)
    server._wake_session_turn(KEY, "a card finished", display_kind="kanban_notice", label="kanban-sweep-wake")
    assert _loaded()
    assert_continued(recovery)


@pytest.mark.parametrize("fields, owed", [
    ({}, True),
    ({"reason": "background_jobs_stopped"}, False),  # not a paused turn
    ({"auto_continue": False}, False),               # the mailbox owns its recovery
])
def test_successor_owes_continuation_reads_the_durable_pause(recovery, fields, owed):
    _edit_marker(recovery.home, **fields)
    assert up.successor_owes_continuation(recovery.home, KEY) is owed
    assert up.successor_owes_continuation(recovery.home, "another-chat") is False


def test_a_pause_whose_writer_lives_is_not_owed_by_a_successor(recovery, monkeypatch):
    monkeypatch.setattr("tui_gateway.turn_marker.marker_writer_state", lambda marker: "alive")
    assert up.successor_owes_continuation(recovery.home, KEY) is False


@pytest.fixture
def profiles_root(recovery, tmp_path_factory, monkeypatch):
    """A real hermes root whose profiles/ the wake enumerates, with the real profile resolver restored."""
    root = tmp_path_factory.mktemp("root")
    (root / "profiles").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(server, "_profile_home", _REAL_PROFILE_HOME)
    monkeypatch.setattr(server, "_served_profile_homes", set())
    activations = []
    monkeypatch.setattr("tui_gateway.launch_profile_policy.activate_multi_profile_hosting",
                        lambda: activations.append(1))
    return types.SimpleNamespace(root=root, activations=activations)


def _profile(root, name):
    home = root / "profiles" / name
    home.mkdir()
    (home / "state.db").touch()
    return home


def test_a_non_conforming_profile_dir_does_not_fail_the_wake(recovery, profiles_root):
    """Review t_01a1ceb9846fd38e: one profiles/ entry that is no valid profile id made every headless wake of an
    unloaded chat return "error" (the parent returned "started")."""
    _profile(profiles_root.root, "Not A Profile!")
    (recovery.home / "desktop" / "interrupted_turns.json").write_text("{}")  # an idle unloaded chat
    verdict = server._wake_session_turn(KEY, "a card finished", display_kind="kanban_notice",
                                        label="kanban-sweep-wake")
    assert verdict == "started"
    assert recovery.done.wait(5) and recovery.calls[0][0] == "a card finished"


def test_a_fresh_pause_in_a_profile_home_is_left_to_its_successor(recovery, profiles_root, monkeypatch):
    monkeypatch.setattr("tui_gateway.turn_marker.marker_writer_state", lambda marker: "dead")  # no real pid probe
    _profile(profiles_root.root, "Not A Profile!")
    work = _profile(profiles_root.root, "work")
    old = time.time() - up.SUCCESSOR_YIELD_UNKNOWN_S - 1
    _edit_marker(recovery.home, started_at=old, paused_at=old)  # the launch home owes nothing
    record_turn_start(work, KEY, "finish the request")
    mark_turn_paused(work, KEY, up.PAUSE_REASON)
    verdict = server._wake_session_turn(KEY, "a card finished", display_kind="kanban_notice",
                                        label="kanban-sweep-wake")
    assert verdict == "busy"
    assert not recovery.done.wait(0.5) and recovery.calls == []
    # The lookup only reads markers: it must not start hosting every profile in this process.
    assert server._served_profile_homes == set() and profiles_root.activations == []
