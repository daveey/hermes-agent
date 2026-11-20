"""A message queued behind the update hold never replaces a live sibling backend's turn marker. [daveey]

hermes-sync skips ``update_hold_queue`` markers as "not running" (a queued message waits FOR the update; counting
it deadlocked the hub's drain on 2026-11-19). With two backends on one HERMES_HOME (#94778), backend A can be
mid-turn on session S while the user's message reaches backend B, where S is idle and the hold refuses it. B's
hold-queue marker used to replace A's live-turn marker under the same key, so the drain stopped seeing A's turn.
"""
import json
import subprocess
import sys

import pytest

from tui_gateway.turn_marker import UPDATE_HOLD_QUEUE_REASON, read_turn_marker, record_turn_start


@pytest.fixture
def sibling():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _seed(home, entry):
    (home / "desktop").mkdir(parents=True, exist_ok=True)
    (home / "desktop" / "interrupted_turns.json").write_text(json.dumps({"S": entry}))


def test_hold_queue_marker_keeps_a_live_siblings_running_turn(tmp_path, sibling):
    import time
    _seed(tmp_path, {"attempts": 0, "prompt": "A's turn", "started_at": time.time(), "auto_continue": True,
                     "writer_pid": sibling})
    assert record_turn_start(tmp_path, "S", "queued on B", reason=UPDATE_HOLD_QUEUE_REASON) is False
    kept = read_turn_marker(tmp_path, "S")
    assert (kept["writer_pid"], kept.get("reason"), kept["prompt"]) == (sibling, None, "A's turn")


def test_hold_queue_marker_still_replaces_dead_paused_or_own_markers(tmp_path):
    import os
    import time
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    for prior in ({"writer_pid": dead.pid},                                        # crashed writer
                  {"writer_pid": os.getpid()},                                     # this backend's own
                  {"writer_pid": dead.pid, "reason": "core_update_pause"}):        # a crashed writer's paused turn
        _seed(tmp_path, {"attempts": 0, "prompt": "old", "started_at": time.time(), "auto_continue": True, **prior})
        assert record_turn_start(tmp_path, "S", "queued", reason=UPDATE_HOLD_QUEUE_REASON) is True
        got = read_turn_marker(tmp_path, "S")
        assert (got["reason"], got["prompt"], got["writer_pid"]) == (UPDATE_HOLD_QUEUE_REASON, "queued", os.getpid())


def test_a_normal_turn_start_is_unchanged(tmp_path, sibling):
    import time
    _seed(tmp_path, {"attempts": 0, "prompt": "A's turn", "started_at": time.time(), "auto_continue": True,
                     "writer_pid": sibling})
    assert record_turn_start(tmp_path, "S", "B runs") is True
    assert read_turn_marker(tmp_path, "S")["prompt"] == "B runs"
