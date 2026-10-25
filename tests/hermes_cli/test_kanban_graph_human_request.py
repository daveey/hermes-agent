"""Persisted asks reach graph diagnostics without becoming authorization."""
from hermes_cli import kanban_db as db
from hermes_cli import kanban_diagnostics as diagnostics
from hermes_cli.kanban_db_connect import connect


REQUEST = {
    "action": "Choose the release window.",
    "why_human": "Only the release owner can choose the customer downtime window.",
    "interaction": "reply",
    "scope": "Staging release window",
    "evidence": "Release checklist leaves the owner-selected downtime window unset.",
}


def test_persisted_human_request_survives_both_graph_directions(tmp_path):
    path = tmp_path / "board.db"
    with connect(path) as conn:
        parent = db.create_task(conn, title="Release decision", assignee="worker",
                                initial_status="blocked", human_request=REQUEST)
        child = db.create_task(conn, title="Release follow-up", assignee="worker", parents=[parent],
                               initial_status="blocked", human_request=REQUEST)
        dependent = db.create_task(conn, title="Deploy after decision", assignee="worker", parents=[parent])
        unknown = db.create_task(conn, title="Investigate", assignee="worker", initial_status="blocked")
        operational_child = db.create_task(conn, title="Investigation follow-up", assignee="worker", parents=[unknown])
    with connect(path) as conn:
        graph = db.task_graph_contexts(conn, [parent, child, dependent, operational_child])
        assert graph[child]["parents"][0]["human_request"] == REQUEST
        children = {task["id"]: task for task in graph[parent]["children"]}
        assert children[child]["human_request"] == REQUEST
        assert children[dependent]["human_request"] is None
        assert graph[operational_child]["parents"][0]["human_request"] is None
        assert db.task_graph_context(conn, child) == graph[child]
        for task_id, expected in ((dependent, True), (operational_child, False)):
            found = diagnostics.compute_task_diagnostics(
                db.get_task(conn, task_id), db.list_events(conn, task_id),
                db.list_runs(conn, task_id), graph=graph[task_id],
            )
            assert ("waiting_behind_user" in {d.kind for d in found}) is expected
        for task_id, status in ((parent, "blocked"), (child, "blocked"), (unknown, "blocked"),
                                (dependent, "todo"), (operational_child, "todo")):
            task = db.get_task(conn, task_id)
            assert task is not None and task.status == status
        assert db.list_runs(conn, parent) == []
        assert db.list_runs(conn, child) == []
