"""Release reconciliation must not consume its own display-deduplicated output."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli.test_kanban_chat_links import ME, kanban_home as kanban_home, live as live  # noqa: PLC0414


def test_origin_display_dedup_does_not_oscillate_release(live, kanban_home, monkeypatch):
    task = kb.create_task(live, title='receipt-only creator', assignee='worker', session_id='placement')
    uid = links.link_uid(task, ME, 'default', 'human', 'created')
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES (?,?,?,'default','human','created',0,1)", (uid, task, ME))
        live.execute('DELETE FROM kanban_sync_outbox')
    origin = dict(id=task, board='default', gateway=ME, profile='default', origin_gateway=ME,
                  origin_profile='default', origin_session='human', session='human', via='origin',
                  key=f'{ME}/default/default/{task}', status='ready', unfinished=True, stale=False)
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [kanban_home / 'kanban.db'])

    from hermes_cli import kanban_origin_adapter, kanban_origin_ledger, profiles
    (kanban_home / 'kanban').mkdir(exist_ok=True)
    (kanban_home / 'kanban' / 'origin-ledger.db').touch()
    monkeypatch.setattr(kanban_origin_adapter, '_gateway', lambda path: ME)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(kanban_origin_ledger, 'project', lambda *a, **kw: [origin])
    monkeypatch.setattr(activity, '_hub_db_path', lambda: None)
    monkeypatch.setattr(activity, '_legacy_remote_links', list)
    activity._reset_cache_for_tests()

    def reconcile():
        activity._reset_cache_for_tests()  # fresh projection on each side of the feedback loop
        return links.reconcile(live, 'default', ME)

    assert activity._remote_links() == []  # the real local linked-row reader deduplicates it
    for _ in range(4):
        counts = reconcile()
        assert counts['changed'] == counts['released'] == 0
        assert live.execute('SELECT released FROM task_chat_links WHERE uid=?', (uid,)).fetchone()[0] == 0
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_chat_links' AND row_uid=?", (uid,)).fetchone()

    # Real producer transitions still apply exactly once, even while the visible
    # projection hides this same local card. No sticky released flag or freeze.
    for state, status in [(True, 'done'), (False, 'ready')]:
        origin.update(unfinished=not state, status=status)
        assert reconcile()['changed'] == 1
        assert live.execute('SELECT released FROM task_chat_links WHERE uid=?', (uid,)).fetchone()[0] == state
        for _ in range(3):
            assert reconcile()['changed'] == 0
        assert live.execute('SELECT released FROM task_chat_links WHERE uid=?', (uid,)).fetchone()[0] == state


@pytest.mark.parametrize('profile', [None, 'default'])
def test_contained_shepherd_reconcile_does_not_fight_pull(live, monkeypatch, profile):
    task = kb.create_task(live, title='held', assignee='worker', session_id='placement')
    uid = links.link_uid(task, ME, profile, 'human', 'shepherd')
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status='archived',containment_episode='retained-episode' WHERE id=?", (task,))
        live.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES (?,?,?,?,'human','shepherd',0,1)", (uid, task, ME, profile))
        live.execute('DELETE FROM kanban_sync_outbox')
    monkeypatch.setattr(activity, '_remote_links', lambda stale_ok=False: [])
    monkeypatch.setattr(activity, 'remote_links_and_origin_rows', lambda stale_ok=False: ([], []))
    for _ in range(4):
        assert links.reconcile(live, 'default', ME)['released'] == 0
        assert live.execute('SELECT released FROM task_chat_links WHERE uid=?', (uid,)).fetchone()[0] == 0
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_chat_links' AND row_uid=?", (uid,)).fetchone()
