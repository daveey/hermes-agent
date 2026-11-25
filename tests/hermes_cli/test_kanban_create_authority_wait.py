"""A refused create waits (pending) on local state that can change, and is terminal only otherwise. [daveey]"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_create_authority as kca
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_origin_owner as owner
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def _links(conn):
    with kb.write_txn(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS task_chat_links (id INTEGER PRIMARY KEY, uid TEXT, "
                     "task_id TEXT NOT NULL, chat_gateway TEXT NOT NULL, chat_profile TEXT, chat_session TEXT NOT NULL, "
                     "kind TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")


def _enabled(home):
    """``kanban.sync.enabled`` on, before the board's first sync (no Board identity yet)."""
    from tests.hermes_cli import kanban_sync_support as support
    support.write_sync_config(home, True)


def _synced(conn, home):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    _enabled(home)
    sync.install_or_remove(conn, str(home / 'kanban.db'))
    client.ensure_identity(conn, 'default', 'fixture')
    _links(conn)


def _refusal(conn, tid, creator=None):
    with pytest.raises(kca.CreateAuthorityRefused) as refused, kb.write_txn(conn):
        kca.require(conn, tid, creator)
    return refused.value


def _answer(exc):
    """What the courier owner answers for this refusal (the real mapping, then the real status rule)."""
    return owner._dispatch_result(owner.origin_refusal(exc, origin=True))


def test_an_owner_with_sync_off_waits(kanban_home):
    """Sync off (or an unreadable config) is a setting, not a fact no one can change: the create waits, and the
    Board's 7-day request expiry ends a wait that never resolves."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='orphan', assignee='default')
        exc = _refusal(conn, tid)
    assert exc.wait == 'origin_owner_sync_off'
    out = _answer(exc)
    assert out['status'] == 'pending' and out['code'] == 'origin_owner_sync_off'


def test_a_not_yet_synced_owner_waits(kanban_home):
    _enabled(kanban_home)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='orphan', assignee='default')
        exc = _refusal(conn, tid)
    assert exc.wait == 'origin_owner_unsynced'
    out = _answer(exc)
    assert out['status'] == 'pending' and out['code'] == 'origin_owner_unsynced'
    assert out['refusal'] == 'scope_authority_missing' and out['handoff_complete'] is False


def test_a_creator_not_synced_yet_waits(kanban_home, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with kbc.connect_closing() as conn:
        _synced(conn, kanban_home)
        tid = kb.create_task(conn, title='child', assignee='default')
        assert _refusal(conn, tid, creator='t_not_here_yet').wait == 'origin_creator_unsynced'


def test_a_synced_owner_with_no_possible_owner_is_terminal(kanban_home, monkeypatch):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with kbc.connect_closing() as conn:
        _synced(conn, kanban_home)
        tid = kb.create_task(conn, title='orphan', assignee='default')
        exc = _refusal(conn, tid)
    assert exc.wait is None
    out = _answer(exc)
    assert out['status'] == 'rejected' and out['code'] == 'scope_authority_missing'
    assert out['created'] is False and out['handoff_complete'] is False


def test_a_bare_selfwork_create_keeps_its_own_rule(kanban_home):
    exc = kca.CreateAuthorityRefused('no authority')
    assert owner.origin_refusal(exc, origin=False) is None
    assert json.loads(owner.origin_refusal(exc, origin=True))['created'] is False


@pytest.mark.parametrize('sync', ['off', 'on_unsynced'])
def test_a_released_creating_chat_is_terminal_even_while_waiting(kanban_home, monkeypatch, sync):
    """Definitive causes first: no sync brings back a released chat, so sync off or an unsynced board does not
    make it wait."""
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    if sync == 'on_unsynced':
        _enabled(kanban_home)
    with kbc.connect_closing() as conn:
        _links(conn)
        tid = kb.create_task(conn, title='orphan', assignee='default')
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,"
                         "created_at) VALUES ('u1',?,'macbook','default','chat-one','created',1,1)", (tid,))
        exc = _refusal(conn, tid)
    assert exc.wait is None and _answer(exc)['status'] == 'rejected'


def test_a_local_creator_whose_sessions_have_not_synced_waits(kanban_home, monkeypatch):
    """Its ``owner_admitted`` events sync separately from the task row: no sessions yet is a wait."""
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with kbc.connect_closing() as conn:
        _synced(conn, kanban_home)
        creator = kb.create_task(conn, title='creator', assignee='default')
        tid = kb.create_task(conn, title='child', assignee='default')
        exc = _refusal(conn, tid, creator=creator)
    assert exc.wait == 'origin_creator_sessions_unsynced' and _answer(exc)['status'] == 'pending'
