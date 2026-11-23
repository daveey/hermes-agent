"""A contended dispatcher is observable without executing a second tick."""
from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_candidate_receipts as receipts
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_dispatch_lock as lock_fixtures

kanban_home = lock_fixtures.kanban_home
conn = lock_fixtures.conn


@pytest.mark.parametrize('contended_open', [False, True])
def test_locked_dry_run_returns_last_pass_without_writing(conn, monkeypatch, contended_open):
    tid = kb.create_task(conn, title='Waiting', assignee='default')
    with receipts.collect(worker='test', now_ms=123000) as capture:
        receipts.note(tid, 'capacity', 'test capacity', stage='claim')
    receipts.persist(conn, capture)
    if contended_open:
        monkeypatch.setattr(conn, '_dispatch_open_contended', True)
    before = conn.total_changes
    with kbc._dispatch_tick_lock(kb.kanban_db_path()) as held:
        assert held
        result = dispatch.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a: pytest.fail('spawn'))
    assert result.skipped_locked
    diagnostic = result.dispatch_diagnostic
    assert diagnostic['reason'] == 'dispatch_lock_contended'
    assert diagnostic['source'] == 'last_completed_pass'
    assert diagnostic['pass_time_ms'] == 123000
    assert diagnostic['receipts'][0]['task_id'] == tid
    assert diagnostic['receipts'][0]['reason'] == 'test capacity'
    assert diagnostic['historical'] is True
    assert conn.total_changes == before


def test_locked_without_history_is_explicit(conn):
    with kbc._dispatch_tick_lock(kb.kanban_db_path()):
        result = dispatch.dispatch_once(conn, dry_run=True)
    assert result.dispatch_diagnostic['reason'] == 'dispatch_lock_contended'
    assert result.dispatch_diagnostic['source'] == 'unavailable'
    assert result.dispatch_diagnostic['last_pass_unavailable'] == 'no_completed_pass'


def test_locked_empty_completed_pass(conn):
    with receipts.collect(worker='empty', now_ms=456000) as capture:
        pass
    receipts.persist(conn, capture)
    with kbc._dispatch_tick_lock(kb.kanban_db_path()):
        result = dispatch.dispatch_once(conn, dry_run=True)
    assert result.dispatch_diagnostic['source'] == 'last_completed_pass'
    assert result.dispatch_diagnostic['pass_time_ms'] == 456000
    assert result.dispatch_diagnostic['receipts'] == []


def test_locked_evidence_read_failure_is_explicit():
    import sqlite3
    from hermes_cli.kanban_dispatch_verdict import locked
    with sqlite3.connect(':memory:') as closed:
        pass
    closed.close()
    result = locked(closed)
    assert result['source'] == 'unavailable'
    assert result['last_pass_unavailable'] == 'evidence_read_failed'
    assert result['error_type'] == 'ProgrammingError'


@pytest.mark.parametrize('as_json', [False, True])
def test_cli_exposes_locked_verdict(conn, monkeypatch, capsys, as_json):
    from hermes_cli import kanban_ops
    from contextlib import contextmanager
    @contextmanager
    def connected():
        yield conn
    monkeypatch.setattr(kbc, 'connect_closing', connected)
    with kbc._dispatch_tick_lock(kb.kanban_db_path()):
        assert kanban_ops._cmd_dispatch(argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=as_json)) == 0
    output = capsys.readouterr().out
    if as_json:
        payload = json.loads(output)
        assert payload['skipped_locked'] is True
        assert payload['dispatch_diagnostic']['reason'] == 'dispatch_lock_contended'
    else:
        assert 'Historical dispatch evidence:' in output
        assert 'dispatch_lock_contended' in output


@pytest.mark.parametrize('refusal', ['claim', 'screen'])
def test_free_slot_refusal_is_never_silent(conn, monkeypatch, all_assignees_spawnable, refusal):
    from hermes_cli import kanban_dispatch_skip as skip
    from hermes_cli import kanban_db_dispatch_admission as admission
    from hermes_cli import kanban_hub_admission_claim as screen
    from tests.hermes_cli.test_kanban_quota_episode import routing
    routing(monkeypatch)
    tid = kb.create_task(conn, title='Ready with free capacity', assignee='default')
    if refusal == 'claim':
        monkeypatch.setattr(admission, 'admitted_claim', lambda *a: None)
        expected = 'claim_deferred'
    else:
        original = screen.screened_lanes
        def deferred(c, dry_run, review_enabled, unjudged):
            ready, review, held = original(c, dry_run, review_enabled, unjudged)
            return [r for r in ready if r['id'] != tid], review, {*held, tid}
        monkeypatch.setattr(screen, 'screened_lanes', deferred)
        expected = 'admission_deferred'
    for _ in range(2):
        result = dispatch.dispatch_once(conn, max_spawn=10, max_in_progress=10,
                                       spawn_fn=lambda *a: pytest.fail('refused card spawned'))
        assert not result.spawned
        assert skip.latest(conn, tid)['reason'] == expected
    events = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='dispatch_skipped'", (tid,)).fetchone()[0]
    assert events == 1


def test_admission_screen_deferral_has_durable_reason(conn):
    from hermes_cli import kanban_dispatch_skip as skip
    tid = kb.create_task(conn, title='Screened', assignee='default')
    result = dispatch.DispatchResult(admission_deferred=[tid])
    assert skip.record_tick(conn, result) == 1
    assert skip.latest(conn, tid)['reason'] == 'admission_deferred'
    assert skip.record_tick(conn, result) == 0  # bounded event history, current observation retained
