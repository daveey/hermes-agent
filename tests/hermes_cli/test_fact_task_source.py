"""Real SQLite -> native hint -> ChangeFeed -> atomic publication. [daveey]"""
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_store import FactStore
from hermes_cli.fact_task_source import TaskContentSource
from tests.hermes_cli import test_kanban_task_change as helpers

home = helpers.home
board = helpers.board
shadow = helpers.shadow


def rows(conn, ids):
    query = "SELECT id,title FROM tasks WHERE id IN (" + ",".join("?" * len(ids)) + ")"
    return ({"id": row[0], "title": row[1]} for row in conn.execute(query, ids))


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("source_name", ["board", "shadow"])
def test_native_task_mutations_publish_without_manual_wakes(source_name, request):
    writer = request.getfixturevalue(source_name)
    path = next(row[2] for row in writer.execute("PRAGMA database_list") if row[1] == "main")
    a = kb.create_task(writer, title="a", assignee="w")
    b = kb.create_task(writer, title="b", assignee="w")
    scope = Scope("install", "profile", source_name)
    store = FactStore()
    condition = threading.Condition()
    passes = []
    failures = []
    membership = set()
    def key(identity):
        return FactKey(scope, "TaskContentInput", identity)
    def commit(adapter, capture):
        nonlocal membership
        present = {row["id"] for row in capture.records}
        deleted = (membership - present) if capture.full else (set(capture.ids) - present)
        store.publish([Fact(key(row["id"]), row, health="fresh") for row in capture.records],
                      deleted_keys=[key(identity) for identity in deleted])
        membership = (membership - deleted) | present
        with condition:
            passes.append(capture)
            condition.notify_all()
    def failed(adapter, error):
        with condition:
            failures.append(error)
            condition.notify_all()
    def until(predicate):
        with condition:
            assert condition.wait_for(predicate, timeout=10), failures
    feed = ChangeFeed(commit, failed, budget=CaptureBudget(max_seconds=2))
    try:
        feed.register(TaskContentSource(SourceId(scope, "tasks.content", "local"), path, rows))
        until(lambda: store.snapshot(scope).get(key(b)) is not None)
        initial = store.snapshot(scope)
        with kbc.write_txn(writer):
            writer.execute("UPDATE tasks SET title='new-a' WHERE id=?", (a,))
            writer.execute("UPDATE tasks SET title='new-b' WHERE id=?", (b,))
        until(lambda: store.snapshot(scope).get(key(b)).value["title"] == "new-b")
        current = store.snapshot(scope)
        assert current.get(key(a)).value["title"] == "new-a"
        assert initial.get(key(a)).value["title"] == "a"
        assert current.get(key(a)).rev == current.get(key(b)).rev
        before = len(passes)
        with kbc.write_txn(writer):
            writer.execute("UPDATE tasks SET last_heartbeat_at=123 WHERE id=?", (a,))
        until(lambda: len(passes) > before)
        assert store.snapshot(scope).rev == current.rev
        with kbc.write_txn(writer):
            writer.execute("DELETE FROM tasks WHERE id=?", (a,))
        until(lambda: store.snapshot(scope).get(key(a)) is None)
        assert store.snapshot(scope).get(key(b)) is not None
        assert failures == []
    finally:
        feed.close()
