"""Exercise old-plugin projection metadata through the real unblock boundary."""
import copy
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_selfwork_exception as sx
from tests.hermes_cli import test_kanban_selfwork_local as helpers

world = helpers.world
nochat_envelope = helpers.nochat_envelope
request_envelope = helpers.request_envelope
home = helpers.home
operation = helpers.operation


@pytest.mark.parametrize("stamped", [True, False])
def test_old_plugin_projection_unblock_requires_adapter_only_for_stamped_ask(home, operation, stamped):
    ask = copy.deepcopy(operation["snapshot"]["ask"])
    if not stamped:
        ask.pop(sx.STAMP)
    with kbc.connect_closing() as conn:
        with helpers.kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked',human_request=? WHERE id=?",
                         (json.dumps(ask), home["task"]))
        before = [tuple(row) for row in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (home["task"],))]
        changed = helpers.kb.unblock_task(
            conn, home["task"],
            event_extra={"user_steps_projection": operation["op"]["receipt"]},
        )
        after = [tuple(row) for row in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (home["task"],))]
        if stamped:
            assert changed is False
            assert helpers.kb.get_task(conn, home["task"]).status == "blocked"
            assert after == before
        else:
            assert changed is True
            assert helpers.kb.get_task(conn, home["task"]).status != "blocked"
            assert after[:len(before)] == before
            assert len(after) > len(before)
