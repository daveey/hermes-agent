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


def _synced(conn, home):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from tests.hermes_cli import kanban_sync_support as support
    support.write_sync_config(home, True)
    sync.install_or_remove(conn, str(home / 'kanban.db'))
    client.ensure_identity(conn, 'default', 'fixture')
    with kb.write_txn(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS task_chat_links (id INTEGER PRIMARY KEY, uid TEXT, "
                     "task_id TEXT NOT NULL, chat_gateway TEXT NOT NULL, chat_profile TEXT, chat_session TEXT NOT NULL, "
                     "kind TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")


def _refusal(conn, tid, creator=None):
    with pytest.raises(kca.CreateAuthorityRefused) as refused, kb.write_txn(conn):
        kca.require(conn, tid, creator)
    return refused.value


def _answer(exc):
    """What the courier owner answers for this refusal (the real mapping, then the real status rule)."""
    return owner._dispatch_result(owner.origin_refusal(exc, origin=True))


def test_a_never_synced_owner_waits(kanban_home):
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
