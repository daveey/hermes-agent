"""A probe finishing during admission cannot enter the current claim pass."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_hub_admission_claim as hac
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_hub_admission_claim as helpers

hub_judge = helpers.hub_judge
gws = helpers.gws
_claim_screen_first = helpers._claim_screen_first


def _park(hub, lane, monkeypatch):
    tid = helpers._hub_card(hub)
    now = int(time.time())
    cmd = "test -f ~/.hermes/cache/ready.flag"
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (tid,))
        kb._append_event(hub.conn, tid, "scheduled", {"wake_at": now + 86400, "wake_check": cmd,
                                                     "resume_status": lane})
        wc.set_check(hub.conn, tid, cmd, 120)
    state = wc.get_state(hub.conn, tid)
    assert state is not None
    assert wc.apply_result(hub.conn, tid, cmd, 1, "", generation=state["generation"],
                           now=now - 49 * 3600, stale_interval=48 * 3600) is None
    # Hold delivery at the screen boundary, without an unrelated real shell worker.
    monkeypatch.setattr(wc, "run_due_wake_checks", lambda *a, **kw: {})
    return tid, state, now


def _deliver(conn, tid, state, now, kind):
    if kind == "continuation_woken":
        from hermes_cli.kanban_continuation import wake_due_continuations
        assert wake_due_continuations(conn, now=now + 86400) == [tid]
    else:
        rc = 0 if kind == "condition_met" else 1
        assert wc.apply_result(conn, tid, state["cmd"], rc, "", generation=state["generation"],
                               now=now, stale_interval=48 * 3600) == kind


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("kind", ["wake_check_stale", "condition_met", "continuation_woken"])
@pytest.mark.parametrize("judged", [False, True])
def test_wake_after_screen_waits_for_next_judged_pass(gws, hub_judge, monkeypatch, lane, kind, judged):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    hub, _worker, svc = gws
    tid, state, now = _park(hub, lane, monkeypatch)
    if judged:
        with kb.write_txn(hub.conn):
            kb._append_event(hub.conn, tid, "hub_admission", {"admit": True, "entry": "claim"})
    real_screen = hac.safe_screen

    def screen_then_wake(conn):
        deferred = real_screen(conn)
        _deliver(conn, tid, state, now, kind)
        return deferred

    monkeypatch.setattr(hac, "safe_screen", screen_then_wake)
    helpers._tick(hub)
    assert helpers._holder(svc, tid) != helpers.HUB
    task = kb.get_task(hub.conn, tid)
    assert task is not None and task.status == lane
    monkeypatch.setattr(hac, "safe_screen", real_screen)
    helpers._tick(hub)
    assert helpers._holder(svc, tid) == helpers.HUB
    events = [row[0] for row in hub.conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN ('hub_admission', 'claimed') ORDER BY id",
        (tid,))]
    assert events[0] == "hub_admission" and "claimed" in events
    assert len(hub_judge.calls) == (0 if judged else 1)
