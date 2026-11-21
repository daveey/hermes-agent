"""Authentic dispatched source custody for independent scheduler fixtures."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_review_model import implementer_models

SOURCE_TITLE = "Fixture implementation source"
AUTHOR = "claude-sonnet-5-5"


def completed_source(conn):
    """Call on an empty eligible board, before queue/cap/race setup."""
    tid = kb.create_task(conn, title=SOURCE_TITLE, assignee="default")
    seen = []
    result = dispatch.dispatch_once(conn, max_spawn=1, spawn_fn=lambda task, ws: seen.append(task.id))
    assert seen == [tid], result
    task = kb.get_task(conn, tid)
    assert task is not None and task.current_run_id is not None
    # Simulated completion output from this actual dispatched source run.
    kb._append_event(conn, tid, "worker_result", {"route_model": AUTHOR, "route_provider": "anthropic"},
                     run_id=task.current_run_id)
    assert kb.request_review(conn, tid, summary="Fixture implementation ready")
    assert implementer_models(conn, kb.get_task(conn, tid)) == {AUTHOR}
    assert kb.complete_task(conn, tid, summary="Fixture source complete")
    return tid


def completed_gateway_source(gateway, **kwargs):
    """Publish and lease a real source run before synced candidate/race setup."""
    gateway.enter()
    conn = gateway.conn
    tid = kb.create_task(conn, title=SOURCE_TITLE, assignee="default", **kwargs)
    gateway.round()
    result = gateway.tick(max_spawn=1)
    assert [row[0] for row in result.spawned] == [tid], result
    task = kb.get_task(conn, tid)
    assert task is not None and task.current_run_id is not None
    kb._append_event(conn, tid, "worker_result", {"route_model": AUTHOR, "route_provider": "anthropic"},
                     run_id=task.current_run_id)
    assert kb.request_review(conn, tid, summary="Fixture implementation ready",
                             expected_run_id=task.current_run_id)
    assert implementer_models(conn, kb.get_task(conn, tid)) == {AUTHOR}
    assert kb.complete_task(conn, tid, summary="Fixture source complete")
    gateway.round()
    return tid


def source_id(conn):
    row = conn.execute("SELECT id FROM tasks WHERE title=? AND status='done'", (SOURCE_TITLE,)).fetchone()
    assert row is not None, "Set up a real dispatched source before candidates"
    return row[0]


def create_review(conn, title, assignee, **kwargs):
    tid = kb.create_task(conn, title=f"Independent review of {title}", assignee=assignee,
                         creator_task_id=source_id(conn), **kwargs)
    assert kb.request_review(conn, tid, summary="Review the completed source")
    task = kb.get_task(conn, tid)
    assert implementer_models(conn, task) == {AUTHOR}
    assert (task.model_override, task.provider_override) == (None, None)
    return tid
