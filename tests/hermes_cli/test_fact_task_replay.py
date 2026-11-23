"""Real SQLite multi-page dirty replay, failure retention and cap controls. [daveey]"""
from dataclasses import replace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.fact_capture import CaptureBudget, CaptureBudgetExceeded, CapturePending, CaptureRefused
from hermes_cli.fact_bootstrap import PagedBootstrap
from tests.hermes_cli import test_fact_capture as helpers
from tests.hermes_cli.test_fact_task_bootstrap import finish

board = helpers.board


def step(pager, budget):
    with pytest.raises(CapturePending):
        pager.capture(None, budget)


def test_dirty_overflow_and_mutations_beyond_closed_fence(board):
    path, writer = board
    keys = sorted(kb.create_task(writer, title=str(i), assignee="w") for i in range(12))
    seen = []
    def hydrate(conn, ids):
        seen.append(ids)
        return helpers.rows(conn, ids)
    pager = PagedBootstrap(path, hydrate)
    budget = CaptureBudget(max_keys=3)
    step(pager, budget)
    with kbc.write_txn(writer):
        writer.execute("UPDATE tasks SET title='new'")
        writer.execute("DELETE FROM tasks WHERE id=?", (keys[0],))
    while not pager.replay:
        step(pager, budget)
    step(pager, budget)  # first dirty window page
    anchor = pager.anchor
    assert pager.fence is not None and pager.position is not None
    with kbc.write_txn(writer):
        writer.execute("UPDATE tasks SET title='newest'")
        writer.execute("UPDATE tasks SET id='renamed' WHERE id=?", (keys[-1],))
    result = finish(pager, budget)
    assert {r['id']: r['title'] for r in result.records} == dict(writer.execute("SELECT id,title FROM tasks"))
    assert result.full and max(map(len, seen)) <= 3
    assert result.after.revision > anchor.revision
    assert pager.capture(None, budget) is result
    with pytest.raises(CaptureRefused, match="unexpected"):
        pager.capture(replace(result.after, revision=-1), budget)


def test_adapts_failed_middle_page_without_discarding_prefix(board):
    path, writer = board
    for i in range(9):
        kb.create_task(writer, title=str(i), assignee="w")
    calls = []
    def hydrate(conn, ids):
        calls.append(ids)
        if len(calls) > 1 and len(ids) > 1:
            raise CaptureBudgetExceeded("injected slow hydration")
        return helpers.rows(conn, ids)
    pager = PagedBootstrap(path, hydrate)
    budget = CaptureBudget(max_keys=4)
    step(pager, budget)
    anchor, last, prefix = pager.anchor, pager.last_id, dict(pager.rows)
    step(pager, budget)
    assert pager.anchor == anchor and pager.last_id == last and pager.rows == prefix
    result = finish(pager, budget)
    assert len(result.records) == 9
    assert calls.count(calls[0]) == 1


def test_cap_latches_without_rescanning_and_recovers_on_source_change(board):
    path, writer = board
    kb.create_task(writer, title='large' * 100, assignee='w')
    calls = []
    def hydrate(conn, ids):
        calls.append(ids)
        return helpers.rows(conn, ids)
    pager = PagedBootstrap(path, hydrate, max_candidate_bytes=10)
    for _ in range(3):
        with pytest.raises(CaptureRefused, match='candidate byte'):
            pager.capture(None, CaptureBudget())
    assert len(calls) == 1 and pager.rows == {}
    with kbc.write_txn(writer):
        writer.execute("DELETE FROM tasks")
    assert finish(pager, CaptureBudget()).records == ()


def test_existing_rev_index_seeks_inside_large_revision_tie(board):
    from hermes_cli import fact_task_replay, kanban_task_change as changes
    _, writer = board
    _, anchor = changes.changed_since(writer, None)
    revision = anchor.revision + 1
    with kbc.write_txn(writer):
        writer.executemany("INSERT INTO kanban_task_change(task_id,rev) VALUES (?,?)",
                           ((f'key-{i}', revision) for i in range(20000)))
        writer.execute("UPDATE kanban_activity_revision SET revision=?", (revision,))
    position = writer.execute("SELECT rev,rowid FROM kanban_task_change WHERE task_id='key-19000'").fetchone()
    ticks = []
    writer.set_progress_handler(lambda: ticks.append(1) or 0, 100)
    try:
        ids, fresh, fence, end = fact_task_replay.page(writer, anchor, position, None, 100)
    finally:
        writer.set_progress_handler(None, 0)
    assert len(ids) == 100 and fresh == fence and end > tuple(position)
    assert len(ticks) < 100, f'late page scanned prefix: {len(ticks) * 100} VM steps'

