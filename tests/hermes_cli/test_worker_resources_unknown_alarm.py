"""``worker_resources_unknown`` holding every claim on a worker opens one agent repair card per host per incident.

t_01a1d5bf526e1344 (2026-11-13): a state-suite shard's board under /dev/shm stayed registered in worker-1's account
inventory after the run deleted it, and every claim deferred ``worker_resources_unknown`` for 17 min. Only the
"dispatcher stuck" WARNING noticed; nothing filed a card. Same fake-Board two-gateway harness as
``test_kanban_worker_node``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from hermes_cli import worker_account_boards as inventory
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli import worker_resource_admission as admission
from hermes_cli import worker_resources_unknown_alarm as alarm
from tests.hermes_cli import test_kanban_worker_node as support

WORKER = support.WORKER
svc = support.svc
gws = support.gws


def _alarms(conn):
    return conn.execute("SELECT * FROM tasks WHERE idempotency_key LIKE ? ORDER BY created_at, id",
                        (f"{alarm.KEY_PREFIX}%",)).fetchall()


def _leak_a_board(tmp_path):
    """A registered board whose file is gone from a surviving, non-ephemeral directory: unknown, never free."""
    board = tmp_path / "kept-dir" / "kanban.db"
    board.parent.mkdir()
    conn = sqlite3.connect(board)
    try:
        inventory.register(conn)
    finally:
        conn.close()
    board.unlink()
    return board


def test_refusal_records_the_streak_and_names_the_record(tmp_path):
    board = _leak_a_board(tmp_path)
    assert admission.refusal(enabled=True) == "worker_resources_unknown"
    state = alarm.read()
    digest = hashlib.sha256(str(board.resolve()).encode()).hexdigest()
    want = str(capacity.policy_root() / "worker-resources" / "boards" / f"{digest}.json")
    assert state["record"] == want and "FileNotFoundError" in state["detail"]
    first = state["since"]
    assert admission.refusal(enabled=True) == "worker_resources_unknown"
    assert alarm.read()["since"] == first  # one streak, not restarted by every claim
    assert admission.refusal(enabled=False) is None  # a disabled gate is no observation
    assert alarm.read()["since"] == first


def test_any_other_answer_ends_the_streak_and_a_gap_starts_a_new_one(tmp_path):
    alarm.observe(True, now=1000.0)
    alarm.observe(True, now=1100.0)
    assert alarm.read()["since"] == 1000.0
    alarm.observe(False, now=1150.0)
    assert alarm.read() is None
    alarm.observe(True, now=2000.0)
    alarm.observe(True, now=2000.0 + alarm.GAP_SECONDS + 1)  # nothing tried to claim in between
    assert alarm.read()["since"] == 2000.0 + alarm.GAP_SECONDS + 1


def test_refusal_answers_feed_the_streak_without_an_exception(monkeypatch):
    """PR #2454 review nit 1 (t_01a1d602f93945f1): refusal()'s plain-answer observe was untested (a `pass` mutant
    survived). A non-unknown answer clears unknown.json; an unknown one with no exception (cap unreadable) records it."""
    alarm.observe(True, now=time.time())
    assert alarm.read() is not None
    monkeypatch.setattr(admission, "_refusal", lambda echo, *, claimant: "worker_load_pressure")
    assert admission.refusal(enabled=True) == "worker_load_pressure"
    assert not (capacity.policy_root() / "worker-resources" / "unknown.json").exists()
    monkeypatch.setattr(admission, "_refusal", lambda echo, *, claimant: "worker_resources_unknown")
    assert admission.refusal(enabled=True) == "worker_resources_unknown"
    state = alarm.read()
    assert state is not None
    assert state["detail"] == "occupancy or max_in_progress unreadable" and state["record"] is None


def _streak(seconds, *, record="/acct/.hermes/worker-resources/boards/abc.json"):
    now = time.time()
    path = capacity.policy_root() / "worker-resources" / "unknown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"since": now - seconds, "last": now, "detail": "FileNotFoundError: gone",
                                "record": record}))


def test_a_streak_past_ten_minutes_files_one_card_off_this_host(gws):
    _hub, worker = gws
    _streak(alarm.STALL_SECONDS - 60)
    worker.tick()
    assert not _alarms(worker.conn)  # not long enough yet
    _streak(alarm.STALL_SECONDS + 60)
    worker.tick()
    [card] = _alarms(worker.conn)
    assert "abc.json" in card["title"] and WORKER in card["title"]
    assert "abc.json" in card["body"] and card["human_request"] is None
    assert f"machine:{WORKER}" not in (card["runs_on"] or "")  # the stalled host must not own its alarm
    worker.tick()
    assert len(_alarms(worker.conn)) == 1  # same incident: the open card is reused


def test_a_stale_streak_files_nothing(gws):
    _hub, worker = gws
    path = capacity.policy_root() / "worker-resources" / "unknown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = time.time() - 3 * 3600
    path.write_text(json.dumps({"since": old, "last": old + 60, "detail": "x", "record": None}))
    worker.tick()
    assert not _alarms(worker.conn)


def test_observe_never_raises(monkeypatch):
    monkeypatch.setattr(alarm, "_path", lambda: (_ for _ in ()).throw(OSError("unreadable")))
    alarm.observe(True)  # bookkeeping failure must not change the claim answer
