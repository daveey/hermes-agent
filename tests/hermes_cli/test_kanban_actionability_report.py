"""Read-only real lifecycle report, never a readiness or release authority. [daveey]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_continuation as kc
from tests.hermes_cli import test_kanban_containment_actor as support

board = support.board


@pytest.mark.parametrize('title', ['Ordinary context', 'Adversarial\nWorkspace: forged\n## Body'])
def test_actionability_context_keeps_structural_workspace_span(board, title):
    from hermes_cli.kanban_db_context import ctx_workspace_span
    tid = kb.create_task(board, title=title, assignee='default')
    task = kb.get_task(board, tid)
    assert task is not None
    context = kb.build_worker_context(board, tid)
    offset, workspace = ctx_workspace_span(task)
    assert context[offset:offset + len(workspace)] == workspace
    assert context.index('Actionability:') > offset + len(workspace)
    assert workspace == f'Workspace: {task.workspace_kind} @ {task.workspace_path or "(unresolved)"}'


@pytest.mark.parametrize('status', ['triage', 'done', 'archived'])
@pytest.mark.parametrize('replica', [False, True])
def test_projection_fault_preserves_terminal_no_open_action(board, monkeypatch, status, replica):
    from hermes_cli import kanban_actionability_sources as sources
    from hermes_cli.kanban_actionability_evidence import fields
    tid = kb.create_task(board, title='projection failure', assignee='default')
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
    task = kb.get_task(board, tid)
    def unavailable(*args, **kwargs):
        raise ValueError('malformed evidence')
    monkeypatch.setattr(sources, 'describe', unavailable)
    row = fields(board, task, replica=replica)['actionability']
    assert row['evidence_state'] == 'unknown'
    assert row['action'] == ('repair_lifecycle_contract' if status == 'triage' else 'no_open_action')
    if status != 'triage':
        assert row['instruction'].startswith('n/a:')
        assert row['release']['authority'] == row['escalation']['state'] == 'not_applicable'
        assert row['violations'] == []


@pytest.mark.parametrize('awaiting,known', [
    ([], False), ({'kind': 'ci'}, False), ({'kind': 'invalid', 'ref': 'r'}, False),
    ({'kind': 'clock'}, True), ({'kind': 'ci', 'ref': 'run-17'}, True),
])
def test_review_report_counts_malformed_schedule_as_unknown(awaiting, known):
    from tests.hermes_cli.test_kanban_actionability_contract import complete_fixture, event
    from hermes_cli.kanban_actionability_cli import report
    conn = complete_fixture()
    conn.execute("UPDATE tasks SET status='scheduled' WHERE id='root'")
    event(conn, 'schedule', 'scheduled', {'wake_at': 1000, 'awaiting': awaiting}, 101)
    conn.commit()
    before = list(conn.iterdump())
    result = report(conn, now=200)
    row = next(r for r in result['rows'] if r['task_id'] == 'root')
    assert result['unknown_count'] == 1 + (not known)
    assert row['evidence_state'] == ('known' if known else 'unknown')
    assert row['action'] == ('wait_for_named_producer' if known else 'repair_lifecycle_contract')
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('delivery', ['pending', 'unknown', 'confirmed', 'rejected'])
def test_review_report_propagates_carrier_receipt_custody(delivery):
    from tests.hermes_cli.test_kanban_actionability_contract import held_fixture
    from hermes_cli.kanban_actionability_cli import report
    conn = held_fixture()
    conn.execute("INSERT INTO kanban_failure_commands VALUES('owner','run','request','retain',?,NULL)", (delivery,))
    conn.commit()
    before = list(conn.iterdump())
    result = report(conn, now=200)
    row = next(r for r in result['rows'] if r['task_id'] == 'root')
    pending = delivery in ('pending', 'unknown')
    assert row['owner']['kind'] == ('unknown' if pending else 'task')
    assert row['evidence_state'] == ('unknown' if pending else 'known')
    assert row['release']['authority'] == 'kanban_release'  # held root on a real stop: release would act
    assert list(conn.iterdump()) == before
    conn.close()


def start(conn, tid):
    assert kb.claim_task(conn, tid, claimer='default') is not None
    return kb.get_task(conn, tid).current_run_id


def complete_evidence_schema(conn):
    # A complete optional local custody schema; legacy absence is tested separately.
    from hermes_cli.kanban_failure_release import _ensure_commands
    _ensure_commands(conn)
    if 'uid' not in {r[1] for r in conn.execute('PRAGMA table_info(task_runs)')}:
        conn.execute('ALTER TABLE task_runs ADD COLUMN uid TEXT')


def project(conn, tid, now=None):
    from hermes_cli.kanban_actionability_sources import describe
    return describe(conn, tid, now=now)


def test_parser_command_human_pagination_and_json_cursor_are_exact(board, capsys):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser
    ids = sorted(kb.create_task(board, title='pagination fixture', assignee='default') for _ in range(3))
    parser = build_parser(argparse.ArgumentParser().add_subparsers())
    assert kanban_command(parser.parse_args(['stranded', '--limit', '1'])) == 0
    text = capsys.readouterr().out
    assert text.count('More rows:') == 1
    assert f'More rows: --after {ids[0]}' in text
    assert kanban_command(parser.parse_args(['stranded', '--json', '--limit', '1', '--after', ids[0]])) == 0
    result = json.loads(capsys.readouterr().out)
    assert [r['task_id'] for r in result['rows']] == [ids[1]]
    assert result['total_nonterminal'] == result['scanned_count'] == 3
    assert result['next_after'] == ids[1] and result['has_more'] is True
    assert kanban_command(parser.parse_args(['stranded', '--limit', '1', '--after', ids[1]])) == 0
    text = capsys.readouterr().out
    assert ids[2] in text and 'More rows:' not in text


def test_registered_report_bypasses_initialization(board, monkeypatch, capsys):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser
    tid = kb.create_task(board, title='queued report control', assignee='default')
    parser = build_parser(argparse.ArgumentParser().add_subparsers())
    args = parser.parse_args(['stranded', '--json'])
    monkeypatch.setattr(kb, 'init_db', lambda: pytest.fail('read-only report initialized the board'))
    assert kanban_command(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['total_nonterminal'] == report['scanned_count'] == 1
    assert report['rows'][0]['task_id'] == tid
    assert report['rows'][0]['owner'] == {'kind': 'assignee', 'refs': ['default']}
    assert report['rows'][0]['action'] == 'execute_current_scope'
    assert report['rows'][0]['evidence_state'] == 'unknown'  # no live admission evidence


def test_stopped_scope_wins_over_status_comments_and_notices(board):
    from hermes_cli import kanban_spend_budget as budget
    from tests.hermes_cli import test_kanban_spend_budget as spend_helpers
    tid = kb.create_task(board, title='stopped report fixture', assignee='default')
    spend_helpers._runs(board, tid, 2, usage={'cache_read_tokens': 1})
    assert budget.hold(board, tid, lim=budget.Limits(1, 1)) == 'triage'
    with kb.write_txn(board):
        kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped'})
    before = project(board, tid)
    kb.add_comment(board, tid, 'fixture', 'fresh comment is not progress')
    with kb.write_txn(board):
        kb._append_event(board, tid, 'decision_requested', {'reason': 'notice, not execution clearance'})
    after = project(board, tid)
    assert before['contract_key'] == after['contract_key']
    assert before['escalation']['at'] == after['escalation']['at']
    assert after['action'] == 'decide_scope'
    assert after['release']['authority'] == 'kanban_release'  # named; comments and notices still never release it
    assert kb.claim_task(board, tid) is None


def test_scheduled_deadline_follows_finite_window_not_report_clock(board):
    tid = kb.create_task(board, title='finite timer report', assignee='default')
    run = start(board, tid)
    result = kc.continue_task(board, tid, checkpoint='bounded clock window', resume_after_seconds=60,
                              expected_run_id=run)
    assert result['ok']
    current = project(board, tid, now=result['wake_at'] - 1)
    assert current['action'] == 'wait_for_named_producer'
    assert current['blocker']['trigger']
    overdue = project(board, tid, now=result['wake_at'] + 601)
    assert 'scheduled_wake_overdue' in overdue['violations']
    assert overdue['action'] == 'repair_lifecycle_contract'
    assert current['contract_key'] == overdue['contract_key']
    assert current['escalation']['at'] == overdue['escalation']['at']


def test_missing_run_is_repair_not_execution_permission(board):
    tid = kb.create_task(board, title='lost run report', assignee='default')
    start(board, tid)
    # Historical impossible row: inject only into this disposable board, do not mock the reader.
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET current_run_id=NULL WHERE id=?', (tid,))
    row = project(board, tid)
    assert row['action'] == 'repair_lifecycle_contract'
    assert 'running_without_current_run' in row['violations']


def test_dependencies_name_human_parent_and_terminal_satisfaction(board):
    complete_evidence_schema(board)
    parent = kb.create_task(board, title='dependency source', assignee='default')
    child = kb.create_task(board, title='dependency waiter', assignee='default', parents=[parent])
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='blocked',human_request=? WHERE id=?",
                      (json.dumps({'action': 'Supply fixture step', 'why_human': 'fixture', 'scope': 'fixture',
                                   'interaction': 'reply', 'evidence': 'fixture'}), parent))
    row = project(board, child)
    assert row['action'] == 'satisfy_dependency'
    assert parent in row['blocker']['prerequisite']
    assert 'Supply fixture step' in row['blocker']['trigger']
    assert row['dependencies'][0]['owner']['refs'] == ['default']
    assert kb.archive_task(board, parent)
    assert project(board, child)['dependencies'] == []


def test_ownerless_legacy_is_visible_and_never_gets_a_fresh_deadline(board):
    tid = kb.create_task(board, title='legacy unassigned draft', triage=True)
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET assignee=NULL WHERE id=?', (tid,))
    first = project(board, tid, now=10000000000)
    later = project(board, tid, now=10000001000)
    assert first['owner']['kind'] == 'unavailable'
    assert 'owner_missing' in first['violations']
    assert first['contract_key'] == later['contract_key']
    assert first['escalation']['at'] == later['escalation']['at']


def test_report_exact_counts_pagination_and_full_database_no_writes(board):
    from hermes_cli.kanban_actionability_cli import report
    ids = sorted(kb.create_task(board, title=f'report row {n}', assignee='default') for n in range(5))
    before = list(board.iterdump())
    page = report(board, limit=2, now=10000000000)
    assert page['total_nonterminal'] == page['scanned_count'] == 5
    assert page['has_more'] is True and page['next_after'] == ids[1]
    second = report(board, limit=2, after=page['next_after'], now=10000000000)
    third = report(board, limit=2, after=second['next_after'], now=10000000000)
    assert [r['task_id'] for p in (page, second, third) for r in p['rows']] == ids
    assert third['has_more'] is False and third['next_after'] is None
    assert page['per_status'] == {'ready': 5}
    assert page['unknown_count'] == 5  # no fabricated admission observation
    assert list(board.iterdump()) == before


@pytest.mark.parametrize('json_output', [True, False])
def test_subprocess_parser_report_preserves_database_bytes(board, tmp_path, json_output):
    kb.create_task(board, title='subprocess report', assignee='default')
    path = board.execute('PRAGMA database_list').fetchone()[2]
    def digest():
        from pathlib import Path
        return {suffix: hashlib.sha256(Path(path + suffix).read_bytes()).hexdigest()
                for suffix in ('', '-wal') if Path(path + suffix).exists()}
    before = digest()
    logical = list(board.iterdump())
    # SQLite read-only readers may update SHM reader slots; SHM is not semantic database data.
    arguments = ['stranded', '--json'] if json_output else ['stranded']
    code = ('import argparse, socket; from hermes_cli.kanban_parser import build_parser; '
            'from hermes_cli.kanban import kanban_command; from hermes_cli import kanban_db as kb; '
            'defender=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("init/network forbidden")); '
            'kb.init_db=defender; socket.create_connection=defender; '
            'p=build_parser(argparse.ArgumentParser().add_subparsers()); '
            f'raise SystemExit(kanban_command(p.parse_args({arguments!r})))')
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'HERMES_KANBAN_DB': path},
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    if json_output:
        assert json.loads(result.stdout)['total_nonterminal'] == 1
    else:
        assert '1 nonterminal;' in result.stdout
        assert 'deadline=None' in result.stdout and 'evidence=unknown' in result.stdout
    assert digest() == before
    assert list(board.iterdump()) == logical


def test_legacy_missing_optional_tables_is_unknown_without_migration():
    from hermes_cli.kanban_actionability_cli import report
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE tasks(id TEXT,status TEXT,assignee TEXT,created_at INTEGER);"
                       "INSERT INTO tasks VALUES('legacy','scheduled','default',10);")
    before = list(conn.iterdump())
    result = report(conn, now=100)
    assert result['unknown_count'] == 1
    assert result['rows'][0]['action'] == 'repair_lifecycle_contract'
    assert 'scheduled_wake_missing' in result['rows'][0]['violations']
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('cause', ['safety_refusal', 'scope_stopped'])
@pytest.mark.parametrize('human_step', [False, True])
def test_all_held_parents_have_no_recovery_carrier(board, cause, human_step):
    from tests.hermes_cli import test_kanban_actionability_dependencies as deps
    complete_evidence_schema(board)
    parent = kb.create_task(board, title='Held prerequisite', assignee='default')
    child = kb.create_task(board, title='Waiter', assignee='default', parents=[parent])
    deps.restrict(board, parent)
    with kb.write_txn(board):
        kb._append_event(board, parent, 'decision_hold_restricted', {'cause': cause})
        if human_step:
            from tests.hermes_cli.test_kanban_actionability_counts import REQUEST
            board.execute('UPDATE tasks SET human_request=? WHERE id=?', (json.dumps(REQUEST), child))
    before = list(board.iterdump())
    row = project(board, child)
    assert 'dependency_recovery_unavailable' in row['violations']
    assert row['dependencies'][0]['task_id'] == parent
    assert row['dependencies'][0]['restricted'] is True
    assert row['action'] == ('supply_recorded_human_step' if human_step else 'satisfy_dependency')
    assert row['release']['authority'] == 'unavailable'
    assert list(board.iterdump()) == before
    assert kb.claim_task(board, child) is None


def test_renewed_done_parent_is_not_stranded(board, monkeypatch):
    complete_evidence_schema(board)
    from tests.hermes_cli import test_kanban_actionability_dependencies as deps
    parent, child, _, _ = deps.renewed_and_done(board, monkeypatch)
    row = project(board, child)
    assert row['dependencies'] == []
    assert 'dependency_recovery_unavailable' not in row['violations']
    assert project(board, parent)['action'] == 'no_open_action'


@pytest.mark.parametrize('payload', ['[]', '{', 'null'])
def test_malformed_parent_evidence_is_unknown_not_absence(board, payload):
    parent = kb.create_task(board, title='Legacy parent', assignee='default')
    child = kb.create_task(board, title='Legacy waiter', assignee='default', parents=[parent])
    with kb.write_txn(board):
        kb._append_event(board, parent, 'spend_budget_triaged', {'cause': 'scope_stopped'})
        board.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='spend_budget_triaged'", (payload, parent))
    row = project(board, child)
    assert row['evidence_state'] == 'unknown'
    assert row['dependencies'][0]['evidence_state'] == 'unknown'
    assert 'dependency_recovery_unavailable' not in row['violations']


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_terminal_show_context_projection_is_explicit(board, status):
    from hermes_cli.kanban_shadow_view import show_details
    tid = kb.create_task(board, title='Terminal diagnostics', assignee='default')
    assert (kb.complete_task(board, tid, summary='done') if status == 'done' else kb.archive_task(board, tid))
    task = kb.get_task(board, tid)
    before = list(board.iterdump())
    assert project(board, tid)['action'] == 'no_open_action'
    assert show_details(board, task, None, 'remote')['actionability']['action'] == 'no_open_action'
    assert 'no_open_action' in kb.build_worker_context(board, tid)
    assert list(board.iterdump()) == before


def test_report_snapshot_pins_counts_across_writer_commit(board):
    from hermes_cli.kanban_actionability_cli import report
    from hermes_cli.kanban_actionability_sources import Reader
    from unittest.mock import patch
    path = board.execute('PRAGMA database_list').fetchone()[2]
    tid = kb.create_task(board, title='Snapshot original', assignee='default')
    original = Reader.task
    changed = False
    def read(self, task_id):
        nonlocal changed
        if not changed:
            changed = True
            with sqlite3.connect(path) as writer:
                writer.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        return original(self, task_id)
    with patch.object(Reader, 'task', read):
        result = report(board, now=777)
    assert changed
    assert result['observed_at'] == 777
    assert result['per_status'] == {'ready': 1}
    assert result['rows'][0]['status'] == 'ready'
    assert project(board, tid)['status'] == 'done'


def test_real_claim_loss_reclaim_and_stale_run_controls(board):
    tid = kb.create_task(board, title='Disposable never-spawned worker', assignee='default')
    run = start(board, tid)
    assert project(board, tid)['action'] == 'execute_current_scope'
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET claim_expires=1 WHERE id=?', (tid,))
    assert kb.release_stale_claims(board, signal_fn=lambda *a: pytest.fail('no live worker may be signalled')) == 1
    assert kb.get_task(board, tid).status == 'ready'
    assert board.execute('SELECT ended_at FROM task_runs WHERE id=?', (run,)).fetchone()[0] is not None
    assert project(board, tid)['action'] == 'execute_current_scope'
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='running',current_run_id=? WHERE id=?", (run, tid))
    assert 'running_without_current_run' in project(board, tid)['violations']
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert 'running_without_current_run' not in project(board, tid)['violations']


@pytest.mark.parametrize('carrier', ['open', 'held', 'closed', 'unknown'])
def test_administrative_carrier_is_not_dependency_release(board, carrier):
    complete_evidence_schema(board)
    from tests.hermes_cli import test_kanban_actionability_dependencies as deps
    parent = kb.create_task(board, title='Held root', assignee='default')
    child = kb.create_task(board, title='Unfinished acceptance', assignee='default', parents=[parent])
    owner = kb.create_task(board, title='Decision carrier', assignee='default')
    deps.restrict(board, parent)
    if carrier == 'held':
        deps.restrict(board, owner)
    elif carrier == 'closed':
        assert kb.complete_task(board, owner, summary='administration ended')
    elif carrier == 'unknown':
        with kb.write_txn(board):
            kb._append_event(board, owner, 'spend_budget_triaged', {})
            board.execute("UPDATE task_events SET payload='[]' WHERE task_id=? AND kind='spend_budget_triaged'", (owner,))
    episode = board.execute("SELECT id FROM task_events WHERE task_id=? AND kind='decision_hold_restricted'", (parent,)).fetchone()[0]
    with kb.write_txn(board):
        kb._append_event(board, parent, 'spend_budget_decision_ready', {
            'episode': str(episode), 'producer': {'kind': 'supervisor', 'task_id': owner}})
    row = project(board, child)
    assert ('dependency_recovery_unavailable' in row['violations']) == (carrier in ('held', 'closed'))
    assert row['dependencies'][0]['restricted'] is True
    assert row['release']['authority'] == 'unavailable'
    assert kb.unsatisfied_parents(board, child)
    assert kb.get_task(board, child).status == 'todo'


def test_show_and_worker_context_never_write_or_scan_report(board, monkeypatch):
    from tools.kanban_show import _detail
    from hermes_cli import kanban_actionability_cli as cli
    monkeypatch.setattr(cli, 'report', lambda *a, **kw: pytest.fail('per-card path scanned report'))
    tid = kb.create_task(board, title='Read-only writable connection', assignee='default')
    task = kb.get_task(board, tid)
    writes = []
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_ALTER_TABLE}
    def authorize(action, *args):
        if action in denied:
            writes.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    before = list(board.iterdump())
    board.set_authorizer(authorize)
    for replica in (False, True):
        detail = _detail(kb, board, task, {}, replica=replica)
        assert detail['actionability']['task_id'] == tid
        assert 'Actionability:' in detail['worker_context']
    assert not writes
    assert list(board.iterdump()) == before


@pytest.mark.parametrize('corrupt', ['body', 'late_receipt', 'parent_table'])
def test_real_policy_reader_failures_are_parent_unknown(board, monkeypatch, corrupt):
    from tests.hermes_cli import test_kanban_actionability_dependencies as deps
    parent, child, episode, comment = deps.renewed_and_done(board, monkeypatch)
    with kb.write_txn(board):
        if corrupt == 'body':
            board.execute('UPDATE task_comments SET body=? WHERE id=?', (sqlite3.Binary(b'legacy bytes'), comment))
        elif corrupt == 'late_receipt':
            kb._append_event(board, parent, 'decision_retained', {'episode_uid': episode, 'disposition': {'reason': 'scope_stopped'}})
        else:
            board.execute('ALTER TABLE task_comments RENAME TO unavailable_comments')
    before = list(board.iterdump())
    row = project(board, child)
    assert row['evidence_state'] == 'unknown'
    assert row['dependencies'][0]['task_id'] == parent
    assert row['dependencies'][0]['evidence_state'] == 'unknown'
    assert 'dependency_recovery_unavailable' not in row['violations']
    assert list(board.iterdump()) == before


def test_report_covers_every_nonterminal_status_and_excludes_terminal(board):
    from hermes_cli.kanban_actionability import NONTERMINAL
    from hermes_cli.kanban_actionability_cli import report
    for status in (*NONTERMINAL, 'done', 'archived'):
        tid = kb.create_task(board, title='Status ' + status, assignee='default')
        with kb.write_txn(board):
            board.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
        assert project(board, tid)['status'] == status
    result = report(board, limit=1, now=1000)
    assert result['total_nonterminal'] == len(NONTERMINAL)
    assert result['per_status'] == dict.fromkeys(NONTERMINAL, 1)
    assert result['returned_count'] == 1 and result['has_more']


@pytest.mark.parametrize('damage', ['nested_episode', 'missing_custody'])
def test_parent_uncertainty_prevents_known_recovery_incident(board, damage):
    from tests.hermes_cli import test_kanban_actionability_dependencies as deps
    complete_evidence_schema(board)
    parent = kb.create_task(board, title='Parent', assignee='default')
    child = kb.create_task(board, title='Child', assignee='default', parents=[parent])
    deps.restrict(board, parent)
    with kb.write_txn(board):
        if damage == 'nested_episode':
            kb._append_event(board, parent, 'decision_hold_restricted',
                             {'cause': 'scope_stopped', 'episode': ['invalid']})
        else:
            board.execute('ALTER TABLE kanban_failure_commands RENAME TO missing_custody')
    row = project(board, child)
    assert row['dependencies'][0]['evidence_state'] == 'unknown'
    assert 'dependency_recovery_unavailable' not in row['violations']


def test_unknown_carrier_with_recorded_deadline_is_not_known(board):
    from tests.hermes_cli import test_kanban_actionability_dependencies as deps
    complete_evidence_schema(board)
    carrier = kb.create_task(board, title='Carrier with damaged evidence', assignee='default')
    held = kb.create_task(board, title='Held scope', assignee='default')
    deps.restrict(board, held)
    with kb.write_txn(board):
        kb._append_event(board, held, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': 'episode'})
        kb._append_event(board, held, 'decision_retained', {'episode': 'episode', 'next_action': {
            'owner': {'kind': 'task', 'task_id': carrier}, 'escalate_at': 12345678900}})
        kb._append_event(board, carrier, 'decision_retained', {})
        board.execute("UPDATE task_events SET payload='[]' WHERE task_id=? AND kind='decision_retained'", (carrier,))
    row = project(board, held)
    assert row['owner'] == {'kind': 'unknown', 'refs': [carrier]}
    assert row['escalation']['at'] == 12345678900
    assert row['evidence_state'] == 'unknown'


