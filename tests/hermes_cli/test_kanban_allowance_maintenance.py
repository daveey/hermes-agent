"""Real home decision/notice recovery of an exhausted Board allowance."""
import json
import time

import pytest

from hermes_cli import kanban_allowance_maintenance as maintenance
from hermes_cli import kanban_allowance_observation as observation
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from tests.hermes_cli import test_kanban_spend_allowance as fixtures
from tests.hermes_cli import test_kanban_allowance_observation as reads

kanban_home = fixtures.kanban_home
primary = fixtures.primary


@pytest.fixture(autouse=True)
def isolated_cursors():
    maintenance._CURSORS.clear()
    yield
    maintenance._CURSORS.clear()


def _prepare(primary, monkeypatch, **changes):
    tid, uid = fixtures._released(primary)
    with fixtures.kbc.write_txn(primary):
        primary.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    reads._hub(monkeypatch, primary, reads._reply(primary, tid, uid, **changes))
    return tid


def test_exhaustion_creates_one_real_decision_episode_and_route(primary, monkeypatch):
    tid = _prepare(primary, monkeypatch)
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    assert maintenance.sweep(primary) == [tid]
    assert fixtures.kb.get_task(primary, tid).status == "triage"
    stop = decision.pending_episode(primary, tid)
    assert stop is not None
    payload = json.loads(stop["payload"])
    assert payload["allowance"]["remaining"] == 0
    assert payload["allowance"]["review_uid"] == fixtures.REVIEW
    episode = stop.get("uid") or str(stop["id"])
    notice = primary.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                             (tid, decision.NOTICE)).fetchone()
    assert notice is not None
    assert json.loads(notice[0])["episode"] == episode
    assert json.loads(notice[0])["producer"]["kind"] in {"supervisor", "chats", "unavailable"}
    before = primary.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    maintenance._CURSORS.clear()
    assert maintenance.sweep(primary) == []
    assert primary.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0] == before


@pytest.mark.parametrize("status", ["running", "done", "archived", "blocked", "scheduled", "todo"])
def test_no_other_lifecycle_state_is_triaged(primary, monkeypatch, status):
    tid = _prepare(primary, monkeypatch)
    observed = observation.read(primary, tid)
    with fixtures.kbc.write_txn(primary):
        primary.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    assert not maintenance.triage(primary, tid, observed)
    assert fixtures.kb.get_task(primary, tid).status == status


def test_live_claim_cannot_be_triaged(primary, monkeypatch):
    tid = _prepare(primary, monkeypatch)
    observed = observation.read(primary, tid)
    with fixtures.kbc.write_txn(primary):
        primary.execute("UPDATE tasks SET claim_lock='other',claim_expires=? WHERE id=?", (int(time.time())+100, tid))
    assert not maintenance.triage(primary, tid, observed)
    assert fixtures.kb.get_task(primary, tid).status == "ready"


def test_successor_budget_wins_before_write(primary, monkeypatch):
    tid = _prepare(primary, monkeypatch)
    observed = observation.read(primary, tid)
    fixtures._override(primary, tid, runs=10, runs_basis=fixtures.allowance.BASIS, review_uid="new")
    assert not maintenance.triage(primary, tid, observed)
    assert fixtures.kb.get_task(primary, tid).status == "ready"


def test_nonexhausted_never_triaged_and_paces_network(primary, monkeypatch):
    tid = _prepare(primary, monkeypatch, used=9, remaining=1)
    calls = reads._hub(monkeypatch, primary, reads._reply(primary, tid, fixtures.allowance.expected(primary, tid)["budget_event_uid"], used=9, remaining=1))
    assert maintenance.sweep(primary) == []
    assert maintenance.sweep(primary) == []
    assert sum(call[0] == "read" for call in calls) == 1
    assert fixtures.kb.get_task(primary, tid).status == "ready"


def test_actual_dispatch_maintenance_calls_allowance_recovery_before_reviews(primary, monkeypatch):
    from hermes_cli import kanban_dispatch_maintenance as dispatch
    calls = []
    monkeypatch.setattr(maintenance, "sweep", lambda conn: calls.append("allowance"))
    monkeypatch.setattr(reviewer, "start_due", lambda conn: calls.append("review"))
    dispatch._start_spend_reviews(primary)
    assert calls == ["allowance", "review"]


def test_cursor_advances_and_wraps_without_starving_healthy_prefix(primary, monkeypatch):
    tids = sorted(_prepare(primary, monkeypatch, used=0, remaining=10) for _ in range(3))
    clock = [100.0]
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(maintenance, "BATCH", 2)
    calls = []
    monkeypatch.setattr(observation, "read", lambda conn, tid, **kw: calls.append(tid))
    assert maintenance.sweep(primary) == []
    assert calls == tids[:2]
    clock[0] += maintenance.CADENCE
    maintenance.sweep(primary)
    assert calls == tids
    clock[0] += maintenance.CADENCE
    maintenance.sweep(primary)
    assert calls == tids + tids[:2]


def test_foreign_copy_is_neither_read_nor_written(primary, monkeypatch):
    from hermes_cli import kanban_wake_check
    tid = _prepare(primary, monkeypatch)
    observed = observation.read(primary, tid)
    monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda conn, task: True)
    calls = []
    monkeypatch.setattr(observation, "read", lambda *a, **kw: calls.append(a))
    assert maintenance.sweep(primary) == []
    assert calls == []
    assert not maintenance.triage(primary, tid, observed)
    assert fixtures.kb.get_task(primary, tid).status == "ready"


def test_expired_observation_preserves_ready(primary, monkeypatch):
    tid = _prepare(primary, monkeypatch)
    observed = observation.read(primary, tid)
    monkeypatch.setattr(observation.time, "monotonic", lambda: observed.deadline)
    assert not maintenance.triage(primary, tid, observed)
    assert fixtures.kb.get_task(primary, tid).status == "ready"


def test_sweep_never_reads_under_write_transaction(primary, monkeypatch):
    _prepare(primary, monkeypatch)
    calls = []
    monkeypatch.setattr(observation, "read", lambda *a, **kw: calls.append(a))
    with fixtures.kbc.write_txn(primary):
        assert maintenance.sweep(primary) == []
    assert calls == []
