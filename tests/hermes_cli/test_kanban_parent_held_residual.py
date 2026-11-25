"""Held-parent publication is deduplicated and cannot invalidate a durable release."""
import json

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_release as helpers

board = helpers.board


def sweep(board):
    from hermes_cli.kanban_decision_maintenance import run
    from types import SimpleNamespace
    run(board, SimpleNamespace(), board='default', dry_run=False)


def events(board, child):
    return board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='parent_held'", (child,)).fetchall()


def test_ticks_and_subscription_purge_do_not_repeat_notice(board, monkeypatch):
    from hermes_cli import kanban_hold_audience as audience
    from hermes_cli import kanban_hold_notices as notices
    parent = helpers.spend_stopped(board)
    child = kb.create_task(board, title='waiting child', assignee='default')
    kb.link_tasks(board, parent, child)
    chats = [{'gateway': 'local', 'profile': 'default', 'session': 'owner-chat'}]
    monkeypatch.setattr(audience, 'describe', lambda *_: {'chats': chats, 'creator_task_id': None, 'creator_active': False})
    monkeypatch.setattr(notices, 'destinations', lambda *_: [('tui', c['session']) for c in chats])
    from hermes_cli import kanban_db_notify as notify
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
    sweep(board)
    assert len(events(board, child)) == 1
    args = dict(task_id=child, platform='tui', chat_id='owner-chat', kinds=TERMINAL_KINDS)
    delivered = notify.claim_unseen_events_for_sub(board, **args)[2]
    assert [e.kind for e in delivered] == ['parent_held']
    assert notify.claim_unseen_events_for_sub(board, **args)[2] == []
    with kb.write_txn(board):
        board.execute('UPDATE task_events SET created_at=1 WHERE task_id=?', (child,))
    assert notify.purge_stale_done_notify_subs(board, max_age_days=1) == 1
    snapshot = kb.get_task(board, child)
    for _ in range(8):
        assert notify.purge_stale_done_notify_subs(board, max_age_days=1) == 0
        before = board.total_changes
        sweep(board)
        assert len(events(board, child)) == 1
        assert board.total_changes == before
    assert kb.get_task(board, child).status == snapshot.status
    assert kb.parent_states(board, child) == [(parent, 'triage')]
    assert board.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (child,)).fetchone()[0] == 0
    chats.append({'gateway': 'local', 'profile': 'default', 'session': 'late-owner'})
    sweep(board)
    assert len(events(board, child)) == 2
    sweep(board)
    assert len(events(board, child)) == 2
    with kb.write_txn(board):
        kb._append_event(board, parent, helpers.decision.EVENT, {'reason': 'new stop'})
    sweep(board)
    assert len(events(board, child)) == 3
    payload = json.loads(events(board, child)[-1][0])
    assert payload['parent_id'] == parent and payload['reason'] == 'new stop'


def test_parent_notice_preserves_release_crash_resume(board, monkeypatch):
    child = helpers.spend_stopped(board)
    parent = helpers.spend_stopped(board)
    kb.link_tasks(board, parent, child)
    real = kb.block_task
    monkeypatch.setattr(kb, 'block_task', lambda *a, **kw: False)
    assert helpers.run(board, child)['refusal'] == 'dependency_wait_refused'
    assert helpers.records(board, child) == 1
    sweep(board)
    assert len(events(board, child)) == 1
    monkeypatch.setattr(kb, 'block_task', real)
    out = helpers.run(board, child)
    assert out['outcome'] == 'waiting_on_parents' and out['resumed'], out
    assert kb.get_task(board, child).status == 'todo'
    assert helpers.records(board, child) == 1 and helpers.mandates(board, child) == 1


def test_notice_keeps_guard_escalation_and_fallback_park_clocks(board):
    from hermes_cli import kanban_db_dispatch_guard_notice as guard
    from hermes_cli import kanban_awaiting as awaiting, kanban_continuation as continuation
    tid = kb.create_task(board, title='guard clock', assignee='default')
    with kb.write_txn(board):
        kb._append_event(board, tid, 'respawn_guarded', {'reason': 'active_pr'})
    started = guard.guard_hold_started_at(board, tid)
    with kb.write_txn(board):
        kb._append_event(board, tid, 'parent_held', {})
    assert guard.guard_hold_started_at(board, tid) == started
    assert guard.guard_hold_reason(board, tid) == 'active_pr'
    with kb.write_txn(board):
        kb._append_event(board, tid, awaiting.ESCALATED_EVENT,
                         {'owner_task_id': 'owner', 'awaiting': {'fired_reparks': 4}})
    parked = continuation.park_state(board, tid)['parked_at']
    with kb.write_txn(board):
        kb._append_event(board, tid, 'parent_held', {})
        board.execute("UPDATE task_events SET created_at=created_at+1000 WHERE task_id=? AND kind='parent_held'", (tid,))
    assert awaiting.escalated_owner(board, tid) == 'owner'
    assert awaiting.escalated_fired(board, tid) == 4
    assert continuation.park_state(board, tid)['parked_at'] == parked
    with kb.write_txn(board):
        kb._append_event(board, tid, 'claimed', {})
    assert awaiting.escalated_owner(board, tid) is None
    assert guard.guard_hold_started_at(board, tid) is None


def test_notice_does_not_postpone_stale_triage(board):
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage
    tid = kb.create_task(board, title='old triage', assignee='default', triage=True)
    with kb.write_txn(board):
        board.execute('UPDATE task_events SET created_at=100 WHERE task_id=?', (tid,))
        kb._append_event(board, tid, 'parent_held', {})
        board.execute("UPDATE task_events SET created_at=9999 WHERE task_id=? AND kind='parent_held'", (tid,))
    assert tid in sweep_stale_triage(board, older_than=100, now=10000)


def test_publication_is_home_only_and_dry_run_is_read_only(board, monkeypatch):
    from hermes_cli import kanban_parent_held as held, kanban_wake_check as wake
    from hermes_cli.kanban_decision_maintenance import run
    from types import SimpleNamespace
    parent = helpers.spend_stopped(board)
    child = kb.create_task(board, title='waiting child', assignee='default')
    kb.link_tasks(board, parent, child)
    before = board.total_changes
    run(board, SimpleNamespace(), board='default', dry_run=True)
    assert board.total_changes == before and events(board, child) == []
    monkeypatch.setattr(wake, 'foreign_ids', lambda *_: {child})
    held.sweep(board)
    assert board.total_changes == before and events(board, child) == []
    monkeypatch.setattr(wake, 'foreign_ids', lambda *_: set())
    held.sweep(board)
    assert len(events(board, child)) == 1
    with kb.write_txn(board):
        kb._append_event(board, parent, 'commented', {'reason': 'ordinary chatter'})
    held.sweep(board)
    assert len(events(board, child)) == 1
    with kb.write_txn(board):
        kb._append_event(board, parent, helpers.decision.EVENT, {'reason': 'new episode'})
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
    held.sweep(board)
    assert len(events(board, child)) == 1


def test_failed_publication_rolls_back_and_retries_once(board, monkeypatch):
    from hermes_cli import kanban_hold_notices as notices, kanban_parent_held as held
    parent = helpers.spend_stopped(board)
    child = kb.create_task(board, title='waiting child', assignee='default')
    kb.link_tasks(board, parent, child)
    real = notices.creator

    def fail(*_):
        raise RuntimeError('controlled notification failure')

    monkeypatch.setattr(notices, 'creator', fail)
    held.sweep(board)
    assert events(board, child) == []
    monkeypatch.setattr(notices, 'creator', real)
    held.sweep(board)
    assert len(events(board, child)) == 1
    held.sweep(board)
    assert len(events(board, child)) == 1
    assert kb.parent_states(board, child) == [(parent, 'triage')]


def test_notice_has_actionable_waking_formatter():
    from types import SimpleNamespace
    from gateway import kanban_watchers_notifier as notifier
    assert 'parent_held' in notifier.TERMINAL_KINDS
    assert 'parent_held' in notifier._WAKE_KINDS
    shown, body, markup = notifier._EVENT_FORMATTERS['parent_held'](
        SimpleNamespace(payload={'parent_id': 'parent', 'parent_status': 'triage', 'reason': 'decision'}),
        SimpleNamespace(head='child'))
    assert 'parent' in shown and 'disposition' in body
    assert 'no scope renewal' in body and markup is None
