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
DAY = 24 * 3600  # the healthy-exemption bound (kanban_deploy_window.HEALTHY_MAX_SECONDS)


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_spend_budget as budget

    # Exercise the deploy-wait clock/escalation policy independently of spend admission.
    monkeypatch.setattr(budget, "limits", lambda: budget.Limits(0, 0))
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


def park(conn, tid, op, awaiting=AWAITING, probe=PROBE):
    """One real park; returns (last event, closed run summary)."""
    if op == "schedule":
        assert kb.schedule_task(conn, tid, reason=REASON, awaiting=awaiting, wake_check=probe, check_every=300)
    elif op == "block":
        assert kb.block_task(conn, tid, reason=REASON, kind="time", awaiting=awaiting,
                             wake_check=probe, check_every=300)
    else:
        result = kc.continue_task(conn, tid, checkpoint=REASON, resume_after_seconds=60, max_continuations=40,
                                  awaiting=awaiting, wake_check=probe, check_every=300)
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


def _at(monkeypatch, home, t):
    """Move the clock to ``t`` and publish a fresh, healthy batch whose window is still ahead."""
    monkeypatch.setattr("time.time", lambda: t)
    publish(home, observed_at=t, window_at=t + 3600, state_since=t - 100)


@pytest.mark.parametrize("op", OPS)
def test_healthy_exemption_expires_after_bound_then_escalates_on_third_park(board, monkeypatch, op):
    """A ref that never reaches main: other commits keep the window ahead, so every park is
    'healthy'. Once the run is older than the bound, parks count and the third one escalates."""
    from hermes_cli import kanban_deploy_window as kdw

    assert kdw.HEALTHY_MAX_SECONDS == DAY
    home, conn = board
    tid = running(conn)
    # 22 hourly healthy parks: more than the 20 events prior_same_premise looks at, so the
    # run's start must come from the full history, not the last 20 parks.
    for h in range(22):
        _at(monkeypatch, home, NOW + 3600 * h)
        event, _summary = park(conn, tid, op)
        assert event.kind != kaw.ESCALATED_EVENT
        assert event.payload["awaiting"]["deploy_window"]["healthy"] is True
        assert event.payload["awaiting"]["deploy_window"]["streak_since"] == NOW
        running(conn, tid)
    late = NOW + DAY + 1
    kinds = []
    for i in range(3):
        _at(monkeypatch, home, late + 60 * i)
        event, _summary = park(conn, tid, op)
        kinds.append(event.kind)
        if event.kind == kaw.ESCALATED_EVENT:
            break
        seen = event.payload["awaiting"]["deploy_window"]
        assert seen["healthy"] is False and seen["healthy_expired"] is True
        assert event.payload["wake_at"] == late + 60 * i + 3600 + 900  # floor is kept
        running(conn, tid)
    assert kinds == ["scheduled" if op != "continue" else "continued"] * 2 + [kaw.ESCALATED_EVENT]


@pytest.mark.parametrize("op", OPS)
def test_healthy_exemption_holds_inside_bound(board, monkeypatch, op):
    """Control: just inside the bound, healthy parks stay exempt and never escalate."""
    home, conn = board
    tid = running(conn)
    for t in (NOW, NOW + 3600, NOW + DAY - 60, NOW + DAY, NOW + DAY):
        _at(monkeypatch, home, t)
        event, _summary = park(conn, tid, op)
        assert event.kind != kaw.ESCALATED_EVENT
        assert event.payload["awaiting"]["deploy_window"]["healthy"] is True
        running(conn, tid)


@pytest.mark.parametrize("op", OPS)
def test_bound_counts_from_the_run_after_another_premise(board, monkeypatch, op):
    """A park on another premise ends the run: the bound restarts from the next deploy park."""
    home, conn = board
    tid = running(conn)
    _at(monkeypatch, home, NOW)
    park(conn, tid, op)
    running(conn, tid)
    restart = NOW + DAY
    _at(monkeypatch, home, restart)
    park(conn, tid, op, awaiting={"kind": "external", "ref": "vendor-1234", "why": "vendor queue"},
         probe="test -f /nonexistent-vendor-receipt")
    running(conn, tid)
    for t in (restart + 60, restart + 120, restart + 180):
        _at(monkeypatch, home, t)
        event, _summary = park(conn, tid, op)
        seen = event.payload["awaiting"]["deploy_window"]
        assert seen["healthy"] is True and seen["streak_since"] == restart + 60
        running(conn, tid)


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
