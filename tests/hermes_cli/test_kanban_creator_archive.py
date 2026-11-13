"""Creator cleanup crosses gateways without acquiring general archive authority."""
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_foreign_card as kfc
from hermes_cli import kanban_owner_archive_relay as relay
from hermes_cli import kanban_supervisor_requests as ksr
from tests.hermes_cli import test_kanban_operator_call as helpers

fleet = helpers.fleet


def child(f):
    tid = f.card(f.h, "creator's scratch fixture", creator_task_id=f.caller, workspace_kind="scratch")
    f.board()
    return tid


def call(f, tid, monkeypatch):
    @contextmanager
    def replica(_path):
        yield f.rep

    monkeypatch.setattr(kfc, "open_replica", replica)
    monkeypatch.setenv("HERMES_KANBAN_TASK", f.caller)
    try:
        return relay.worker_archive(f.w, tid, run_id_fn=lambda _: f.run_id)
    finally:
        monkeypatch.delenv("HERMES_KANBAN_TASK")


@pytest.mark.parametrize("status", ["ready", "triage", "done"])
def test_creator_archive_roundtrip(fleet, monkeypatch, status):
    f = fleet
    tid = child(f)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    f.board()
    refusal, out = call(f, tid, monkeypatch)
    assert refusal is None
    assert out["status"] == "archive_pending"
    helpers._round(f)
    assert kb.get_task(f.h, tid).status == "archived"
    assert kb.get_task(f.w, tid) is None
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"


def test_other_creator_refused(fleet, monkeypatch):
    f = fleet
    refusal, out = call(f, f.target, monkeypatch)
    assert refusal and out is None
    assert kb.get_task(f.h, f.target).status != "archived"


def test_home_rechecks_claim_after_submission(fleet, monkeypatch):
    f = fleet
    tid = child(f)
    refusal, out = call(f, tid, monkeypatch)
    assert refusal is None
    ksr.drain(f.w)
    assert kb.claim_task(f.h, tid)
    f.board()
    ksr.drain(f.h)
    f.board()
    ksr.drain(f.w)
    assert kb.get_task(f.h, tid).status == "running"
    assert ksr.get_request(f.w, out["request_id"])["status"] == "rejected"


@pytest.mark.parametrize("creator_status", ["done", "archived"])
def test_triage_cleans_explicit_scratch_archive_request(fleet, creator_status):
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage
    f = fleet
    creator = f.card(f.h, "finished checker")
    tid = f.card(f.h, "scratch fixture", creator_task_id=creator, workspace_kind="scratch", triage=True)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status=? WHERE id=?", (creator_status, creator))
    kb.add_comment(f.h, tid, "worker", f"Run `hermes kanban archive {tid}` as a non-worker caller.")
    from hermes_cli.kanban_triage_cleanup import eligible
    assert eligible(f.h, tid), (kb.get_task(f.h, tid), kb.get_task(f.h, creator),
                               f.h.execute("SELECT payload FROM task_events WHERE task_id=?", (tid,)).fetchall())
    assert tid not in sweep_stale_triage(f.h, older_than=0)
    assert kb.get_task(f.h, tid).status == "archived"
    comment = f.h.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1",
                          (tid,)).fetchone()[0]
    assert "finished creator" in comment


@pytest.mark.parametrize("condition", ["live_creator", "missing_creator", "not_scratch", "claimed", "human",
                                       "dependent", "new_comment", "negation", "other_target", "no_comment"])
def test_triage_cleanup_controls(fleet, condition):
    from hermes_cli.kanban_triage_cleanup import archive_requested_fixture
    f = fleet
    creator = f.card(f.h, "checker")
    tid = f.card(f.h, "fixture", creator_task_id=creator, workspace_kind="scratch", triage=True)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status='done' WHERE id=?", (creator,))
    text = f"Run `hermes kanban archive {tid}` as a non-worker caller."
    if condition == "negation":
        text = "Do not " + text
    if condition == "other_target":
        text = text.replace(tid, creator)
    if condition != "no_comment":
        kb.add_comment(f.h, tid, "worker", text)
    if condition == "new_comment":
        kb.add_comment(f.h, tid, "worker", "Keep this; more work remains.")
    if condition == "dependent":
        f.card(f.h, "dependent", parents=[tid])
    with kb.write_txn(f.h):
        if condition == "live_creator":
            f.h.execute("UPDATE tasks SET status='ready' WHERE id=?", (creator,))
        if condition == "missing_creator":
            f.h.execute("DELETE FROM task_events WHERE task_id=? AND kind='created'", (tid,))
        if condition == "not_scratch":
            f.h.execute("UPDATE tasks SET workspace_kind='dir' WHERE id=?", (tid,))
        if condition == "claimed":
            f.h.execute("UPDATE tasks SET claim_lock='live' WHERE id=?", (tid,))
        if condition == "human":
            f.h.execute("UPDATE tasks SET human_request=? WHERE id=?", ('{"action":"sign in"}', tid))
    assert not archive_requested_fixture(f.h, tid)
    assert kb.get_task(f.h, tid).status == "triage"


def test_creator_archive_refuses_forged_board_provenance(fleet, monkeypatch):
    f = fleet
    tid = child(f)
    _, out = call(f, tid, monkeypatch)
    ksr.drain(f.w)
    f.board()
    from hermes_cli import kanban_operator_call as kox
    uid = f.rep.execute("SELECT uid FROM task_events WHERE kind=?", (kox.REQUEST_KIND,)).fetchone()[0]
    f.origin[uid] = "unrelated-gateway"
    ksr.drain(f.h)
    f.board()
    ksr.drain(f.w)
    assert kb.get_task(f.h, tid).status != "archived"
    assert ksr.get_request(f.w, out["request_id"])["status"] == "rejected"
