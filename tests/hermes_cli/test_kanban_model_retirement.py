"""Owner-side stored policy retirement, without touching execution custody."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kd
from tests.hermes_cli import test_kanban_model_tier as fixtures

board = fixtures.board
PREFIX = 'Historical run instruction (superseded and not active): '


def seed(conn, *, status='blocked', model='grok-4.6', provider='xai-oauth', mandatory=1, body='scope'):
    tid = kb.create_task(conn, title='ordinary work', body='scope', assignee='default')
    conn.execute('UPDATE tasks SET status=?,model_override=?,provider_override=?,model_pin_mandatory=?,body=? WHERE id=?',
                 (status, model, provider, mandatory, body, tid))
    return tid


def sweep(conn, **kwargs):
    from hermes_cli.kanban_model_retirement import sweep as retire
    return retire(conn, **kwargs)


@pytest.mark.parametrize('status', ['todo', 'triage', 'ready', 'scheduled', 'blocked', 'running', 'review'])
def test_open_selector_retired_and_only_policy_changes(board, status, monkeypatch):
    with kbc.connect() as conn:
        tid = seed(conn, status=status)
        before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        runs = list(conn.execute('SELECT * FROM task_runs'))
        notices = []
        monkeypatch.setattr(kb, 'notify_task_updated', lambda c, t, f, **k: notices.append((t, tuple(f))))
        assert sweep(conn) == 1
        after = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        for field in ('model_override', 'provider_override', 'model_pin_mandatory'):
            assert after.pop(field) is None
            before.pop(field)
        # The activity cursor is allowed to advance for a policy edit.
        for field in ('updated_at',):
            before.pop(field, None)
            after.pop(field, None)
        assert after == before
        assert list(conn.execute('SELECT * FROM task_runs')) == runs
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='model_override_cleared'", (tid,)).fetchone()
        assert json.loads(event[0])['reason'] == 'model_policy_no_pins'
        assert conn.execute("SELECT author FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] == 'kanban-dispatcher'
        assert notices == [(tid, ('model_override', 'provider_override', 'model_pin_mandatory'))]
        counts = [conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('task_events', 'task_comments')]
        assert sweep(conn) == 0
        assert counts == [conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('task_events', 'task_comments')]


@pytest.mark.parametrize('model,provider,mandatory,expected', [
    ('class:deep', 'auto', None, ('class:deep', 'auto', None)),
    ('class:deep', 'anthropic', 1, ('class:deep', None, None)),
    (None, 'auto', None, (None, 'auto', None)),
    ('auto', 'auto', 0, (None, 'auto', None)),
    (None, None, 1, (None, None, None)),
])
def test_class_auto_and_mandatory(board, model, provider, mandatory, expected):
    with kbc.connect() as conn:
        tid = seed(conn, model=model, provider=provider, mandatory=mandatory)
        sweep(conn)
        row = conn.execute('SELECT model_override,provider_override,model_pin_mandatory FROM tasks WHERE id=?', (tid,)).fetchone()
        assert tuple(row) == expected


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_terminal_unchanged(board, status):
    with kbc.connect() as conn:
        tid = seed(conn, status=status, body='require_model: true')
        before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        assert sweep(conn) == 0
        assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before


def test_body_exact_line_roundtrip_and_supported_edit(board, monkeypatch):
    from hermes_cli import kanban_db_edit as edits
    with kbc.connect() as conn:
        body = 'scope  \r\n  require_model: true\r\n\nexclude: claude-opus-5-5\nUse only grok-4.6.\rtrailing no newline'
        tid = seed(conn, body=body)
        calls = []
        original = edits.edit_task
        def observed(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)
        monkeypatch.setattr(edits, 'edit_task', observed)
        assert sweep(conn) == 1
        expected = ('scope  \r\n' + PREFIX + json.dumps('  require_model: true') + '\r\n\n'
                    + 'exclude: claude-opus-5-5\n' + PREFIX + json.dumps('Use only grok-4.6.') + '\r'
                    + 'Use only any qualified route.\rtrailing no newline')
        assert kb.get_task(conn, tid).body == expected
        assert len(calls) == 1
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='edited'", (tid,)).fetchone()[0] == 1
        assert sweep(conn) == 0


@pytest.mark.parametrize('field,new', [('model_override', 'different-model'), ('provider_override', 'auto'), ('model_pin_mandatory', None)])
def test_each_exact_selector_cas_atom(board, field, new):
    from hermes_cli.kanban_model_retirement import retire_selectors
    with kbc.connect() as conn:
        tid = seed(conn)
        snapshot = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        conn.execute(f'UPDATE tasks SET {field}=? WHERE id=?', (new, tid))
        assert not retire_selectors(conn, snapshot)
        assert conn.execute(f'SELECT {field} FROM tasks WHERE id=?', (tid,)).fetchone()[0] == new


def test_second_connection_wins_at_transaction_boundary(board, monkeypatch):
    from contextlib import contextmanager
    from hermes_cli.kanban_model_retirement import retire_selectors
    with kbc.connect() as conn, kbc.connect() as other:
        tid = seed(conn)
        snapshot = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        original = kb.write_txn
        injected = []
        @contextmanager
        def race(c, **kwargs):
            if c is conn and not injected:
                injected.append(True)
                kb.set_model_override(other, tid, 'class:deep', 'auto')
            with original(c, **kwargs):
                yield
        monkeypatch.setattr(kb, 'write_txn', race)
        assert not retire_selectors(conn, snapshot)
        assert injected
        assert kb.get_task(conn, tid).model_override == 'class:deep'


def test_notify_observes_committed_row_and_audit(board, monkeypatch):
    import sqlite3
    with kbc.connect() as conn:
        tid = seed(conn, body='require_model: true')
        seen = []
        def observe(c, task, fields, **kwargs):
            assert task == tid
            assert not c.in_transaction
            with sqlite3.connect('file:' + str(kbc._main_db_file(c)) + '?mode=ro', uri=True) as reader:
                assert reader.execute('SELECT model_override FROM tasks WHERE id=?', (task,)).fetchone()[0] is None
                assert reader.execute('SELECT COUNT(*) FROM task_comments WHERE task_id=?', (task,)).fetchone()[0] >= 1
            seen.append(tuple(fields))
        monkeypatch.setattr(kb, 'notify_task_updated', observe)
        assert sweep(conn) == 1
        assert len(seen) == 2


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029', ''])
def test_all_body_line_endings_and_preexisting_evidence(ending):
    from hermes_cli.kanban_model_retirement import retired_body
    accepted = PREFIX + json.dumps('Use only grok-4.6.') + '\nuntouched λ  \n'
    source = accepted + 'Use only grok-4.6. λ  ' + ending
    assert retired_body(source) == (accepted + PREFIX + json.dumps('Use only grok-4.6. λ  ', ensure_ascii=False)
                                    + (ending or '\n') + 'Use only any qualified route. λ  ' + ending)


def test_cas_concurrent_set_model_wins(board):
    from hermes_cli.kanban_model_retirement import retire_selectors
    with kbc.connect() as conn:
        tid = seed(conn)
        snapshot = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        kb.set_model_override(conn, tid, 'class:deep', 'auto')
        assert not retire_selectors(conn, snapshot)
        assert kb.get_task(conn, tid).model_override == 'class:deep'


def test_per_card_fault_and_bounded_rotation(board, monkeypatch):
    from hermes_cli import kanban_model_retirement as retirement
    with kbc.connect() as conn:
        bad = seed(conn)
        good = seed(conn)
        original = retirement.retire_selectors
        def faulty(c, row):
            if row['id'] == bad:
                raise RuntimeError('one malformed legacy card')
            return original(c, row)
        monkeypatch.setattr(retirement, 'retire_selectors', faulty)
        for _ in range(2):
            sweep(conn, limit=1)
        assert kb.get_task(conn, good).model_override is None
        assert kb.get_task(conn, bad).model_override == 'grok-4.6'


def test_real_dispatch_once_retires_even_without_spawn_budget(board):
    with kbc.connect() as conn:
        tid = seed(conn)
        kd.dispatch_once(conn, max_spawn=0, reconcile_orphans=False)
        assert kb.get_task(conn, tid).model_override is None
        assert kb.get_task(conn, tid).model_pin_mandatory is None


def test_running_claim_run_and_session_are_untouched(board):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='live work', assignee='default')
        assert kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET model_override='grok-4.6',provider_override='xai-oauth',model_pin_mandatory=1,session_id='live-session' WHERE id=?", (tid,))
        before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        run = dict(conn.execute('SELECT * FROM task_runs WHERE id=?', (before['current_run_id'],)).fetchone())
        sweep(conn)
        after = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        for key in ('claim_lock', 'claim_expires', 'current_run_id', 'session_id', 'status', 'worker_pid'):
            assert after[key] == before[key]
        assert dict(conn.execute('SELECT * FROM task_runs WHERE id=?', (before['current_run_id'],)).fetchone()) == run


def test_body_cas_and_terminal_races(board):
    from hermes_cli.kanban_model_retirement import retire_body, retire_selectors
    with kbc.connect() as conn:
        tid = seed(conn, body='require_model: true')
        snapshot = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        kb.edit_task(conn, tid, body='new user scope')
        assert not retire_body(conn, snapshot)
        assert kb.get_task(conn, tid).body == 'new user scope'
        conn.execute("UPDATE tasks SET body=?,status='done' WHERE id=?", (snapshot['body'], tid))
        assert not retire_body(conn, snapshot)
        assert not retire_selectors(conn, snapshot)
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
        assert not retire_body(conn, snapshot)
        assert not retire_selectors(conn, snapshot)


def test_audit_failure_rolls_back_and_body_failure_keeps_siblings(board, monkeypatch):
    with kbc.connect() as conn:
        tid = seed(conn, body='require_model: true')
        good = seed(conn, body='require_model: true')
        original = kb.add_comment
        def refuse(c, task, *args):
            if task == tid:
                raise RuntimeError('failed audit')
            return original(c, task, *args)
        monkeypatch.setattr(kb, 'add_comment', refuse)
        assert sweep(conn) == 1
        assert kb.get_task(conn, tid).body == 'require_model: true'
        assert kb.get_task(conn, tid).model_override == 'grok-4.6'
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN ('edited','model_override_cleared')", (tid,)).fetchone()[0] == 0
        assert kb.get_task(conn, good).model_override is None
        assert kb.get_task(conn, good).body.startswith(PREFIX)


def test_dry_dispatch_does_not_retire(board):
    with kbc.connect() as conn:
        tid = seed(conn)
        kd.dispatch_once(conn, dry_run=True, max_spawn=0, reconcile_orphans=False)
        assert kb.get_task(conn, tid).model_override == 'grok-4.6'
