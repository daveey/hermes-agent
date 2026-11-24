"""The fifth renewal without a done/review milestone needs goal-sized disposition."""
from types import SimpleNamespace

import contextlib
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_decision_ask as helpers

kanban_home = helpers.kanban_home


def _tick(conn, monkeypatch, *, dry_run=False):
    from hermes_cli import kanban_decision_maintenance as maintenance
    from hermes_cli import kanban_idle_candidates, kanban_provider_walls, kanban_db_dispatch_quota
    monkeypatch.setattr(kanban_idle_candidates, 'sweep_shared_blockers', lambda *a: None)
    monkeypatch.setattr(kanban_provider_walls, 'tick_upkeep', lambda *a, **kw: None)
    monkeypatch.setattr(kanban_db_dispatch_quota, 'release_recovered_quota_blocks', lambda *a: 0)
    maintenance.run(conn, SimpleNamespace(), board='default', dry_run=dry_run)


def _renew(conn, tid, count):
    with kb.write_txn(conn):
        for n in range(count):
            kb._append_event(conn, tid, 'scope_renewed', {'episode': f'stop-{n}'})


def _alarms(conn):
    return conn.execute("SELECT * FROM tasks WHERE idempotency_key LIKE 'scope-renewal-loop:%'").fetchall()


def test_fifth_renewal_files_one_owned_remediation_without_dependency(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        chat = helpers._session()
        tid = kb.create_task(conn, title='step-sized lander', assignee='default', session_id=chat,
                             require_scope_authority=True)
        _renew(conn, tid, 4)
        _tick(conn, monkeypatch)
        assert not _alarms(conn)
        _renew(conn, tid, 1)
        _tick(conn, monkeypatch)
        alarms = _alarms(conn)
        assert len(alarms) == 1
        alarm = alarms[0]
        assert tid in alarm['body'] and 'goal' in alarm['body'] and 'step-sized' in alarm['body']
        from hermes_cli.kanban_create_authority import has_authority
        assert has_authority(conn, alarm['id'], tid, durable=True)
        assert not kb.parent_ids(conn, alarm['id'])
        assert not kb.child_ids(conn, tid)
        before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        _tick(conn, monkeypatch)
        _renew(conn, tid, 1)
        _tick(conn, monkeypatch)
        assert len(_alarms(conn)) == 1
        assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before


@pytest.mark.parametrize('milestone', ['completed', 'review_requested', 'status'])
def test_milestone_committing_before_alarm_writer_resets_count(kanban_home, monkeypatch, milestone, caplog):
    from hermes_cli import kanban_scope_renewal_alarm as alarm
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='racing milestone', assignee='default',
                             session_id=helpers._session(), require_scope_authority=True)
        _renew(conn, tid, 5)
        writer = kb.write_txn
        injected = False

        @contextlib.contextmanager
        def racing_writer(c, **kwargs):
            nonlocal injected
            with writer(c, **kwargs):
                if not injected:
                    injected = True
                    kb._append_event(c, tid, milestone, {'status': 'review'})
                yield

        monkeypatch.setattr(kb, 'write_txn', racing_writer)
        alarm.sweep(conn)
        assert injected
        assert not caplog.records
        assert not _alarms(conn)


@pytest.mark.parametrize('milestone', ['completed', 'review_requested', 'status'])
def test_milestones_rearm_only_after_five_fresh_renewals(kanban_home, monkeypatch, milestone):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='goal', assignee='default', session_id=helpers._session(),
                             require_scope_authority=True)
        _renew(conn, tid, 5)
        _tick(conn, monkeypatch)
        first = _alarms(conn)[0]['id']
        # Closing the remediation does not authorize duplicates in the same incident.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (first,))
        _renew(conn, tid, 7)
        _tick(conn, monkeypatch)
        assert len(_alarms(conn)) == 1
        with kb.write_txn(conn):
            kb._append_event(conn, tid, milestone, {'status': 'review'})
        _renew(conn, tid, 4)
        _tick(conn, monkeypatch)
        assert len(_alarms(conn)) == 1
        _renew(conn, tid, 1)
        _tick(conn, monkeypatch)
        assert len(_alarms(conn)) == 2


@pytest.mark.parametrize('status', ['done', 'review', 'archived'])
def test_current_milestone_or_retirement_is_not_an_incident(kanban_home, monkeypatch, status):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='retired goal', assignee='default', session_id=helpers._session(),
                             require_scope_authority=True)
        _renew(conn, tid, 5)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
        _tick(conn, monkeypatch)
        assert not _alarms(conn)


def test_dry_run_and_foreign_replica_never_file(kanban_home, monkeypatch):
    from hermes_cli import kanban_wake_check
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='replica goal', assignee='default', session_id=helpers._session(),
                             require_scope_authority=True)
        _renew(conn, tid, 5)
        _tick(conn, monkeypatch, dry_run=True)
        assert not _alarms(conn)
        monkeypatch.setattr(kanban_wake_check, 'foreign_ids', lambda c: {tid})
        _tick(conn, monkeypatch)
        assert not _alarms(conn)


def test_no_authority_rolls_back_only_one_incident(kanban_home, monkeypatch, caplog):
    with kbc.connect_closing() as conn:
        orphan = kb.create_task(conn, title='legacy orphan', assignee='default')
        owned = kb.create_task(conn, title='owned goal', assignee='default', session_id=helpers._session(),
                               require_scope_authority=True)
        _renew(conn, orphan, 5)
        _renew(conn, owned, 5)
        _tick(conn, monkeypatch)
        assert len(_alarms(conn)) == 1
        assert owned in _alarms(conn)[0]['body']
        assert 'scope renewal alarm failed' in caplog.text
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 3


@pytest.mark.parametrize('status', ['done', 'review', 'archived', None])
def test_source_retires_or_disappears_before_writer(kanban_home, monkeypatch, status, caplog):
    from hermes_cli import kanban_scope_renewal_alarm as alarm
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='retiring goal', assignee='default', session_id=helpers._session(),
                             require_scope_authority=True)
        _renew(conn, tid, 5)
        writer = kb.write_txn
        injected = False

        @contextlib.contextmanager
        def racing_writer(c, **kwargs):
            nonlocal injected
            with writer(c, **kwargs):
                if not injected:
                    injected = True
                    if status is None:
                        c.execute('DELETE FROM tasks WHERE id=?', (tid,))
                    else:
                        c.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
                yield

        monkeypatch.setattr(kb, 'write_txn', racing_writer)
        alarm.sweep(conn)
        assert injected and not caplog.records
        assert not _alarms(conn)
