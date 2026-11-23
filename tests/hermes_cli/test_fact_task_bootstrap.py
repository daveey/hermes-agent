"""Paged real SQLite bootstrap and scheduler continuation invariants. [daveey]"""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.fact_capture import CaptureBudget, CapturePending, CaptureRefused
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_task_source import TaskContentSource
from tests.hermes_cli import test_fact_capture as helpers
from tests.hermes_cli.test_fact_feed import Adapter

board = helpers.board


def finish(source, budget, cursor=None):
    for _ in range(30):
        try:
            return source.capture(cursor, budget)
        except CapturePending:
            continue
    pytest.fail("bootstrap failed to converge")


def test_pages_replay_updates_and_deletes_before_atomic_result(board):
    path, writer = board
    keys = sorted(kb.create_task(writer, title=str(i), assignee="w") for i in range(7))
    sizes = []
    def hydrate(conn, ids):
        sizes.append(len(ids))
        return helpers.rows(conn, ids)
    source = TaskContentSource("board", path, hydrate)
    budget = CaptureBudget(max_keys=3)
    with pytest.raises(CapturePending):
        source.capture(None, budget)  # overflow, no retained prefix
    with pytest.raises(CapturePending):
        source.capture(None, budget)  # first page
    with kbc.write_txn(writer):
        writer.execute("UPDATE tasks SET title='updated' WHERE id=?", (keys[0],))
        writer.execute("DELETE FROM tasks WHERE id=?", (keys[1],))
    result = finish(source, budget)
    actual = {row["id"]: row["title"] for row in result.records}
    expected = {row[0]: row[1] for row in writer.execute("SELECT id,title FROM tasks")}
    assert actual == expected and result.full
    assert max(sizes) <= 3
    # A failed publisher sees the same immutable candidate, even after writes.
    with kbc.write_txn(writer):
        writer.execute("UPDATE tasks SET title='later' WHERE id=?", (keys[0],))
    assert source.capture(None, budget) is result
    delta = source.capture(result.after, budget)
    assert not delta.full and delta.records[0]["title"] == "later"


def test_candidate_budget_discards_partial_state(board):
    from hermes_cli.fact_bootstrap import PagedBootstrap
    path, writer = board
    kb.create_task(writer, title="x" * 100, assignee="w")
    pager = PagedBootstrap(path, helpers.rows, max_candidate_bytes=20)
    with pytest.raises(CaptureRefused, match="candidate byte"):
        pager.capture(None, CaptureBudget())
    assert pager.rows == {} and pager.anchor is None


def test_lost_coverage_cannot_publish_staged_prefix(board):
    path, writer = board
    for i in range(4):
        kb.create_task(writer, title=str(i), assignee="w")
    source = TaskContentSource("board", path, helpers.rows)
    budget = CaptureBudget(max_keys=2)
    for _ in range(2):
        with pytest.raises(CapturePending):
            source.capture(None, budget)
    with kbc.write_txn(writer):
        writer.execute("DROP TRIGGER kanban_activity_rev_tasks_update")
    with pytest.raises(CaptureRefused, match="coverage"):
        source.capture(None, budget)
    assert source._bootstrap.rows == {}


def test_pending_yields_to_other_source_and_never_publishes_or_advances():
    hot, other = Adapter(), Adapter()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    order, errors = [], []
    def capture(cursor):
        assert cursor is None
        if len(hot.calls) == 1:
            entered.set()
            assert release.wait(3)
            hot.wake()  # a trailing hint cannot preserve this source's priority
            raise CapturePending()
        return SimpleNamespace(after=1)
    hot.capture_hook = capture
    def commit(adapter, captured):
        order.append(adapter)
        if len(order) == 2:
            done.set()
    feed = ChangeFeed(commit, lambda *args: errors.append(args))
    try:
        feed.register(hot)
        assert entered.wait(3)
        feed.register(other)
        release.set()
        assert done.wait(3)
    finally:
        release.set()
        feed.close()
    assert order == [other, hot] and not errors
    assert hot.calls == [None, None]
