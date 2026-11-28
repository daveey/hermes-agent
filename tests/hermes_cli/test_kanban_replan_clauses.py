"""Defensive seam and concurrency controls for replan admission."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_auto_replan as rp
from tests.hermes_cli import test_kanban_auto_replan as helpers

kanban_home = helpers.kanban_home


def test_pending_parent_is_due_even_below_threshold():
    assert rp.State('x', 0, 8, (), 'parent').due


def test_portable_run_keys_and_placeholder_exclusion(kanban_home):
    # A supplied row seam exercises portable identities despite local legacy schemas.
    class Rows:
        def execute(self, sql, args):
            assert "status != 'pending_sync'" in sql
            return SimpleNamespace(fetchall=lambda: [
                {'uid': 'portable', 'id': 1}, {'uid': '', 'id': 2}, {'id': 3}])
    assert set(rp._run_keys(Rows(), 'task')) == {'portable', 'local:2', 'local:3'}


@pytest.mark.parametrize('mandate', [None, {}, {'runs': True, '_id': 1},
                                   {'runs': 0, '_id': 1}, {'runs': -9, '_id': 1}])
def test_invalid_or_nonpositive_mandate_cannot_raise_threshold(kanban_home, monkeypatch, mandate):
    from hermes_cli import kanban_release as release
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn, 0)
        monkeypatch.setattr(release, 'current_mandate', lambda *a: mandate)
        assert rp._threshold(conn, tid, {'old': {'started_at': 0}}, ()) == 8


def test_absent_release_event_and_absent_task(kanban_home, monkeypatch):
    from hermes_cli import kanban_release as release
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(release, 'current_mandate', lambda *a: {'runs': 99, '_id': -999})
        assert rp._threshold(conn, 'missing', {}, ()) == 8


def test_completed_baseline_not_counted_again_in_release_allowance(kanban_home, monkeypatch):
    from hermes_cli import kanban_release as release
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn, 0)
        kb._append_event(conn, tid, 'test_release', {})
        event = conn.execute('SELECT id,created_at FROM task_events WHERE task_id=? ORDER BY id DESC', (tid,)).fetchone()
        monkeypatch.setattr(release, 'current_mandate', lambda *a: {'runs': 8, '_id': event[0]})
        rows = {'baseline': {'started_at': event[1]}, 'fresh': {'started_at': event[1]}}
        assert rp._threshold(conn, tid, rows, ('baseline',)) == 9


@pytest.mark.parametrize('guard', ['foreign', 'frozen', 'held', 'pending', 'missing', 'status', 'run', 'lock'])
def test_transaction_rechecks_each_admission_guard(kanban_home, monkeypatch, guard):
    from hermes_cli import kanban_wake_check as wake
    from hermes_cli import kanban_home_transfer as transfer
    from hermes_cli import kanban_containment_connections as containment
    from hermes_cli import kanban_spend_decision as spend
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        calls = [0]
        def late(*a):
            calls[0] += 1
            return calls[0] > 1
        if guard == 'foreign':
            monkeypatch.setattr(wake, 'foreign_copy', late)
        elif guard == 'frozen':
            monkeypatch.setattr(transfer, 'frozen', late)
        elif guard in ('held', 'pending'):
            monkeypatch.setattr(containment if guard == 'held' else spend, guard, lambda *a: True)
        else:
            real = kb.get_task
            def observed(c, target):
                task = real(c, target)
                if target != tid:
                    return task
                if guard == 'missing':
                    return None
                changes = {'status': {'status': 'blocked'}, 'run': {'current_run_id': 123},
                           'lock': {'claim_lock': 'other', 'claim_expires': 2**40}}
                return replace(task, **changes[guard])
            monkeypatch.setattr(kb, 'get_task', observed)
        assert rp.hold(conn, tid) is None
        assert not kb.parent_ids(conn, tid)
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_outer_frozen_short_circuit_before_state(kanban_home, monkeypatch):
    from hermes_cli import kanban_home_transfer as transfer
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        monkeypatch.setattr(transfer, 'frozen', lambda *a: True)
        def unexpected(*a):
            pytest.fail('must not read state from frozen home')
        monkeypatch.setattr(rp, 'state', unexpected)
        assert rp.hold(conn, tid) is None


@pytest.mark.parametrize('after', [None, rp.State('initial', 7, 8, ())])
def test_counter_rechecked_after_write_transaction_entry(kanban_home, monkeypatch, after):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        original = rp.state(conn, tid)
        values = iter([original, after])
        monkeypatch.setattr(rp, 'state', lambda *a: next(values))
        assert rp.hold(conn, tid) is None
        assert not kb.parent_ids(conn, tid)


def test_below_threshold_outer_check_does_not_enter_transaction(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn, 7)
        @contextmanager
        def unexpected(*a):
            pytest.fail('not due: must not enter writer')
            yield
        monkeypatch.setattr(kb, 'write_txn', unexpected)
        assert rp.hold(conn, tid) is None


@pytest.mark.parametrize('lock,expires', [(None, 2**40), ('old', None), ('old', 0), ('old', 1)])
def test_only_live_owned_claim_blocks(kanban_home, lock, expires):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET claim_lock=?,claim_expires=? WHERE id=?', (lock, expires, tid))
        assert rp.hold(conn, tid)


def test_existing_parent_is_relinked_not_recreated(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = rp.hold(conn, tid)
        # Model readiness refreshed independently of a still-open prerequisite.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert rp.hold(conn, tid) == parent
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2


def test_existing_idempotency_key_is_adopted(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = kb.create_task(conn, title='Earlier atomic episode', assignee='default',
                                idempotency_key=f'replan:{tid}:initial')
        assert rp.hold(conn, tid) == parent
        assert kb.parent_ids(conn, tid) == [parent]


@pytest.mark.parametrize('assignee', ['specialist', None])
def test_replan_preserves_assignee_or_uses_default(kanban_home, assignee):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET assignee=? WHERE id=?', (assignee, tid))
        parent = rp.hold(conn, tid)
        assert kb.get_task(conn, parent).assignee == (assignee or 'default')


def test_running_parent_must_not_apply_before_completion(kanban_home, monkeypatch):
    from hermes_cli import kanban_replan_apply as apply
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = rp.hold(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        def unexpected(*a):
            pytest.fail('cannot apply a non-done parent')
        monkeypatch.setattr(apply, 'apply', unexpected)
        assert rp.hold(conn, tid) == parent


def test_published_receipt_does_not_reset_unfinished_parent(kanban_home, monkeypatch):
    from hermes_cli import kanban_replan_apply as apply
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        parent = rp.hold(conn, tid)
        monkeypatch.setattr(apply, 'published', lambda *a: True)
        current = rp.state(conn, tid)
        assert current.parent == parent and current.count == 8


def test_no_parent_does_not_query_or_apply_a_null_parent(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        real = rp._parent_status
        def status(c, parent):
            assert parent is not None, 'do not query absent prerequisite'
            return real(c, parent)
        monkeypatch.setattr(rp, '_parent_status', status)
        assert rp.hold(conn, tid)


@pytest.mark.parametrize('dry_run', [False, True])
def test_budget_refusal_reports_exact_parent_without_dry_run_writes(kanban_home, dry_run):
    from hermes_cli.kanban_db_dispatch_admission import budget_refused
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn)
        result = SimpleNamespace(no_progress_held=[])
        assert budget_refused(conn, tid, result, dry_run=dry_run) is (not dry_run)
        if dry_run:
            assert not kb.parent_ids(conn, tid)
            assert result.no_progress_held == []
        else:
            parent, = kb.parent_ids(conn, tid)
            assert result.no_progress_held == [(tid, f'replan:{parent}')]


def test_budget_admits_below_threshold_without_replan_reason(kanban_home):
    from hermes_cli.kanban_db_dispatch_admission import budget_refused
    with kbc.connect_closing() as conn:
        tid = helpers._card(conn, 7)
        result = SimpleNamespace(no_progress_held=[])
        assert budget_refused(conn, tid, result, dry_run=False) is False
        assert result.no_progress_held == []


def test_tombstone_table_optional_on_partial_store(kanban_home, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_foreign_card as foreign
    @contextmanager
    def absent(*a):
        yield None
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(sync, 'sync_enabled', lambda: True)
        monkeypatch.setattr(foreign, 'open_replica', absent)
        conn.execute('DROP TABLE IF EXISTS kanban_sync_tombstones')
        assert rp._parent_status(conn, 'missing') == 'unknown'
