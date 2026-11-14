"""Creator cleanup authorization at the tool and canonical transport seams."""
import json
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as calls
from hermes_cli import kanban_supervisor_requests as requests
from tests.hermes_cli import test_kanban_creator_archive_adversarial as helpers

fleet = helpers.fleet


@pytest.mark.parametrize("field", ["target_task_id", "caller"])
def test_changed_canonical_request_never_archives(fleet, monkeypatch, field):
    f = fleet
    tid = helpers.child(f)
    _, out = helpers.submit(f, tid, monkeypatch)
    requests.drain(f.w)
    f.board()
    event = f.rep.execute("SELECT uid,payload FROM task_events WHERE kind=?", (calls.REQUEST_KIND,)).fetchone()
    payload = json.loads(event[1])
    payload[field] = f.target if field == "target_task_id" else {**payload["caller"], "task_id": tid}
    with kb.write_txn(f.rep):
        f.rep.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), event[0]))
    requests.drain(f.h)
    assert kb.get_task(f.h, tid).status != "archived"
    assert kb.get_task(f.h, f.target).status != "archived"
    f.board()
    requests.drain(f.w)
    assert helpers.request_status(f, out) == "rejected"


def test_done_target_with_dependent_is_refused_at_home(fleet, monkeypatch):
    f = fleet
    tid = helpers.child(f)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    f.board()
    _, out = helpers.submit(f, tid, monkeypatch)
    requests.drain(f.w)
    f.board()
    f.card(f.h, "unfinished dependent", parents=[tid])
    requests.drain(f.h)
    f.board()
    requests.drain(f.w)
    assert helpers.request_status(f, out) == "rejected"
    assert kb.get_task(f.h, tid).status == "done"


def test_tool_archive_forwards_creator_request(fleet, monkeypatch):
    from hermes_cli import kanban_foreign_card as foreign
    from tools import kanban_tools as tools
    f = fleet
    tid = helpers.child(f)

    @contextmanager
    def board(_slug):
        yield kb, f.w

    monkeypatch.setattr(tools, "_board", board)
    monkeypatch.setattr(tools, "_worker_run_id", lambda _: f.run_id)
    monkeypatch.setattr(foreign, "open_replica", lambda _p: helpers._rep(f))
    monkeypatch.setenv("HERMES_KANBAN_TASK", f.caller)
    out = json.loads(tools._handle_archive({"task_id": tid}))
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert out["status"] == "archive_pending"
    helpers.helpers._round(f)
    assert kb.get_task(f.h, tid).status == "archived"
