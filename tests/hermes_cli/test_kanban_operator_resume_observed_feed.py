"""A worker's real ``active_pr`` hold reaches the home over the Board feed, and the home's resume accepts it.

``test_kanban_operator_resume_observed`` writes the promoted/respawn_guarded events by hand. Here nothing is
hand-built: the worker's own dispatcher records the hold on its kept copy, the in-memory Board feed carries it
to the home, and ``hermes kanban resume`` (the real argparse path) runs on the home while the home's own guard
has released (its GitHub read says MERGED). The worker, whose read still says OPEN, then claims. [daveey]
"""
from __future__ import annotations

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_operator_resume as resume
from hermes_cli.kanban_observed_pr_hold import observed
from tests.hermes_cli import test_kanban_edit_operator_fields as ops
from tests.hermes_cli import test_kanban_operator_resume_worker_copy as copy
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
clock = copy.clock


def test_real_worker_hold_fed_home_lets_home_resume_and_worker_claim(isolate_usage_accounting, gws, clock, monkeypatch, capsys):
    hub, worker = gws
    tid = copy._held_on_worker(gws, clock)  # the worker's dispatch_once recorded the hold
    hub.enter()
    holds = hub.conn.execute("SELECT origin_gateway, payload FROM task_events WHERE task_id=? "
                             "AND kind='respawn_guarded'", (tid,)).fetchall()
    assert holds and all(h["origin_gateway"] == support.WORKER for h in holds)  # fed, not written at home
    assert all('"active_pr"' in h["payload"] for h in holds)
    monkeypatch.setattr(prs, "pr_state", lambda _: "MERGED")  # the home's read: its own guard releases
    assert kbd.check_respawn_guard(hub.conn, tid) is None
    assert observed(hub.conn, tid)  # only the worker's fed observation qualifies the resume
    clock[0] += 60
    assert ops._main(["resume", tid, "--reason", "worker still holds its PR"]) == 0
    assert "Resumed" in capsys.readouterr().out
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")  # the worker's read: still open
    hub.round()
    worker.round()
    worker.enter()
    assert resume.EVENT in copy._kinds(worker.conn, tid)
    assert kbd.check_respawn_guard(worker.conn, tid) is None
    clock[0] += 5
    worker.tick()
    assert copy._status(worker.conn, tid) == "running"
