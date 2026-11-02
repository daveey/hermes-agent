"""Host-local core deploy window evidence through the real parking transactions. [daveey]

The hub's hermes-sync publishes ``<root>/cache/core-update/window.json`` (``HOST`` there is
``socket.gethostname().split('.')[0]``). A deploy wait parked behind a healthy batch window
must not escalate as a repeated premise, and its wake is floored to the batch's drain end; stale,
missing, foreign or malformed evidence changes nothing, and overdue unchanged batches still
escalate.
"""
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc

NOW = int(datetime(2026, 11, 2, 22, tzinfo=UTC).timestamp())
WINDOW = NOW + 3600  # 23:00 UTC = 3:00 PM PST
AWAITING = {"kind": "deploy", "ref": "abcdef123", "why": "core release"}
REASON = "SCHEDULED_UNTIL=2026-11-02T22:01:00Z core release pending"
PROBE = "test -f /nonexistent-core-release-receipt"
OPS = ["schedule", "block", "continue"]


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setattr("time.time", lambda: NOW)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield home, conn
    kwc._shutdown_for_tests()


def publish(home, **changes):
    data = dict(version=1, host=socket.gethostname().split(".")[0], observed_at=NOW,
                window_at=WINDOW, state="batching", state_since=NOW - 100, drain_bound_s=900)
    data.update(changes)
    path = home / "cache/core-update/window.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return data


def running(conn, tid=None):
    if tid is None:
        tid = kb.create_task(conn, title="Verify core release", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def park(conn, tid, op, awaiting=AWAITING):
    """One real park; returns (last event, closed run summary)."""
    if op == "schedule":
        assert kb.schedule_task(conn, tid, reason=REASON, awaiting=awaiting, wake_check=PROBE, check_every=300)
    elif op == "block":
        assert kb.block_task(conn, tid, reason=REASON, kind="time", awaiting=awaiting,
                             wake_check=PROBE, check_every=300)
    else:
        result = kc.continue_task(conn, tid, checkpoint=REASON, resume_after_seconds=60, max_continuations=20,
                                  awaiting=awaiting, wake_check=PROBE, check_every=300)
        assert result["ok"], result
    run = kb.latest_run(conn, tid)
    return kb.list_events(conn, tid)[-1], run.summary if run else None


def park_until_escalated(conn, tid, op, home=None, states=None, awaiting=AWAITING, n=4):
    """Park up to ``n`` times (re-claiming between); returns the kinds of the parking events."""
    kinds = []
    for i in range(n):
        if states:
            publish(home, **states[i])
        event, _summary = park(conn, tid, op, awaiting)
        kinds.append(event.kind)
        if event.kind == kaw.ESCALATED_EVENT:
            break
        running(conn, tid)
    return kinds


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("named", [False, True])
def test_future_window_floors_real_parks_and_preserves_probe(board, monkeypatch, op, named):
    home, conn = board
    publish(home)
    if named:  # a named profile reads the ROOT home's cache
        profile = home / "profiles/worker"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}\n")
        monkeypatch.setenv("HERMES_HOME", str(profile))
    tid = running(conn)
    for _ in range(4):
        event, summary = park(conn, tid, op)
        assert event.kind in ("scheduled", "continued")
        assert event.payload["wake_at"] == WINDOW + 900
        assert event.payload["wake_check"] == PROBE
        assert "core update window at 3:00 PM PST" in summary
        assert event.payload["awaiting"]["deploy_window"]["healthy"] is True
        assert kaw.prior_same_premise(conn, tid, kaw.premise_hash(AWAITING, REASON)) == 0
        running(conn, tid)


@pytest.mark.parametrize("op", OPS)
def test_stale_window_still_escalates(board, op):
    home, conn = board
    publish(home, observed_at=NOW - 1201)
    tid = running(conn)
    assert park_until_escalated(conn, tid, op)[-1] == kaw.ESCALATED_EVENT


@pytest.mark.parametrize("op", OPS)
def test_missing_window_still_escalates(board, op):
    _home, conn = board
    tid = running(conn)
    assert park_until_escalated(conn, tid, op) == ["scheduled" if op != "continue" else "continued"] * 2 + [
        kaw.ESCALATED_EVENT]


@pytest.mark.parametrize("op", OPS)
def test_unchanged_overdue_batch_escalates_but_keeps_drain_floor(board, op):
    home, conn = board
    overdue = dict(window_at=NOW - 600, drain_bound_s=3600, state_since=NOW - 900)
    publish(home, observed_at=NOW - 30, **overdue)
    tid = running(conn)
    event, summary = park(conn, tid, op)
    assert event.payload["wake_at"] == NOW - 600 + 3600
    assert event.payload["awaiting"]["deploy_window"]["healthy"] is False
    assert "core update window" in summary
    running(conn, tid)
    # Fresh observations of the same unchanged overdue batch are not a change.
    kinds = park_until_escalated(conn, tid, op, home, states=[dict(overdue, observed_at=NOW - k) for k in (5, 1, 0)])
    assert kinds[-1] == kaw.ESCALATED_EVENT and len(kinds) == 2


@pytest.mark.parametrize("op", OPS)
def test_healthy_park_is_exempt_after_two_unhealthy_parks(board, op):
    home, conn = board
    tid = running(conn)
    assert park_until_escalated(conn, tid, op, n=2) == ["scheduled" if op != "continue" else "continued"] * 2
    publish(home)  # the batch is now published and ahead: the third park is not a repeat
    event, _summary = park(conn, tid, op)
    assert event.kind != kaw.ESCALATED_EVENT and event.payload["wake_at"] == WINDOW + 900
    running(conn, tid)
    (home / "cache/core-update/window.json").unlink()  # evidence gone: the unhealthy streak resumes
    assert park(conn, tid, op)[0].kind == kaw.ESCALATED_EVENT


@pytest.mark.parametrize("op", OPS)
def test_overdue_batch_whose_state_changes_never_escalates(board, op):
    home, conn = board
    states = [dict(window_at=NOW - 600, state=s, state_since=NOW - 500 + 50 * i)
              for i, s in enumerate(("batching", "draining", "batching", "draining", "draining"))]
    tid = running(conn)
    kinds = park_until_escalated(conn, tid, op, home, states=states, n=5)
    assert kaw.ESCALATED_EVENT not in kinds and len(kinds) == 5


@pytest.mark.parametrize("op", OPS)
def test_foreign_host_window_is_ignored(board, op):
    home, conn = board
    publish(home, host="some-other-host")
    tid = running(conn)
    event, summary = park(conn, tid, op)
    assert event.payload["wake_at"] == NOW + 60 and "core update window" not in summary
    running(conn, tid)
    assert park_until_escalated(conn, tid, op)[-1] == kaw.ESCALATED_EVENT


@pytest.mark.parametrize("bad", [
    "{not json", "[]", {"version": 2}, {"host": 7}, {"observed_at": "now"}, {"observed_at": True},
    {"observed_at": NOW + 30}, {"window_at": None}, {"state": 3}, {"state_since": NOW + 30},
    {"drain_bound_s": -1}, {"drain_bound_s": 1.5e12}, {"state": "idle"},
])
def test_malformed_or_unusable_window_changes_nothing(board, bad):
    home, conn = board
    if isinstance(bad, str):
        path = home / "cache/core-update/window.json"
        path.parent.mkdir(parents=True)
        path.write_text(bad)
    else:
        publish(home, **bad)
    tid = running(conn)
    event, summary = park(conn, tid, "schedule")
    if bad == {"state": "idle"}:  # valid but not batching/draining: floored, not exempt
        assert event.payload["awaiting"]["deploy_window"]["healthy"] is False
    else:
        assert event.payload["wake_at"] == NOW + 60 and "deploy_window" not in event.payload["awaiting"]
        assert "core update window" not in summary
    running(conn, tid)
    assert park_until_escalated(conn, tid, "schedule")[-1] == kaw.ESCALATED_EVENT


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("kind", [{"kind": "ci", "ref": "123456"}, {"kind": "external", "ref": "vendor-1234",
                                                                      "why": "vendor queue"}])
def test_non_deploy_waits_ignore_a_healthy_window(board, op, kind):
    home, conn = board
    publish(home)
    tid = running(conn)
    event, summary = park(conn, tid, op, awaiting=kind)
    assert event.payload["wake_at"] == NOW + 60 and "deploy_window" not in event.payload["awaiting"]
    assert "core update window" not in summary
    running(conn, tid)
    assert park_until_escalated(conn, tid, op, awaiting=kind)[-1] == kaw.ESCALATED_EVENT
