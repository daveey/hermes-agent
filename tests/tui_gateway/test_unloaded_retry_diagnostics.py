"""Actual card fanout retry-site provenance, with unchanged immediate deadlines."""
import json
import sqlite3
import pytest

from hermes_cli import loop_accounting as la
from tui_gateway import notice_fanout as fanout
from tui_gateway.notice_fanout_result import FanoutResult


@pytest.fixture(autouse=True)
def reset(record_property):
    la.reset()
    yield
    record_property('counter_snapshot', json.dumps(la.counters(), sort_keys=True))
    la.reset()


@pytest.mark.parametrize('reason', ['enumeration_failed', 'set_cursor_failed', 'top_gt_cursor', 'board_error', 'remote_error'])
def test_real_retry_sites_preserve_earliest_deadline(monkeypatch, tmp_path, reason):
    from hermes_cli import kanban_db, kanban_db_connect
    from tui_gateway import notice_fanout_result, notice_fanout_state
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE task_events(id INTEGER)')
    conn.execute('INSERT INTO task_events VALUES(2)')
    monkeypatch.setattr(fanout, '_board_slugs', lambda kb: ['board'])
    monkeypatch.setattr(fanout, '_board_idle', lambda *a: False)
    from tui_gateway import notice_triage_clock
    monkeypatch.setattr(notice_triage_clock, 'local', lambda *a, **k: (set(), None))
    monkeypatch.setattr(fanout, '_changed_tasks', lambda *a: ([], 1))
    monkeypatch.setattr(fanout, '_fan_out_changes', lambda *a: [])
    monkeypatch.setattr(fanout, '_remote_change_pass', lambda *a: FanoutResult())
    monkeypatch.setattr(kanban_db, 'kanban_db_path', lambda slug: tmp_path / slug)
    monkeypatch.setattr(kanban_db_connect, 'connect', lambda **k: conn)
    monkeypatch.setattr(notice_fanout_state, 'set_cursor', lambda *a: True)
    monkeypatch.setattr(notice_fanout_result.time, 'monotonic', lambda: 100)
    if reason != 'top_gt_cursor':
        conn.execute('DELETE FROM task_events')
    if reason == 'set_cursor_failed':
        monkeypatch.setattr(notice_fanout_state, 'set_cursor', lambda *a: False)
    def fail(*a):
        raise OSError(reason)
    if reason == 'enumeration_failed':
        monkeypatch.setattr(fanout, '_board_slugs', fail)
    if reason == 'board_error':
        monkeypatch.setattr(fanout, '_changed_tasks', fail)
    if reason == 'remote_error':
        monkeypatch.setattr(fanout, '_remote_change_pass', fail)
    result = fanout.card_change_pass(session_db=lambda *a: None)
    assert result.incomplete
    assert result.next_deadline == (100 if reason == 'top_gt_cursor' else 101)
    assert result.retry_origin == (result.next_deadline, 'card_change_pass', reason)
    assert la.counters()['unloaded.diag.retry.site.' + reason]['runs'] == 1
    conn.close()
    assert list(result) == []
