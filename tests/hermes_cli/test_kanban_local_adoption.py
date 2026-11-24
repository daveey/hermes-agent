"""Local decision adoption never lends interactive authority to machine runs."""
import argparse
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_notify import add_notify_sub
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli import kanban_origin_owner as owner
from hermes_cli import kanban_spend_decision as spend


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_KANBAN_DB',
                'HERMES_KANBAN_BOARD', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_SESSION_ID', 'chat')
    with sqlite3.connect(tmp_path / 'state.db') as db:
        db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, created_source TEXT)')
        db.execute("INSERT INTO sessions VALUES ('chat','desktop','desktop')")
    kb.init_db()
    with connect_closing() as conn:
        tid = kb.create_task(conn, title='Timer card')
        add_notify_sub(conn, task_id=tid, platform='tui', chat_id='chat',
                          delivery_metadata={'reason': 'shepherd'})
        yield conn, tid, tmp_path


def adopt(tid, capsys):
    args = argparse.Namespace(kanban_action='origin-adopt', local=True, task_id=tid,
                              request_file=None, board='default')
    owner.command(args)
    return json.loads(capsys.readouterr().out)


def test_shepherd_spend_stop_then_local_adoption(world, capsys):
    conn, tid, _ = world
    count = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    before = spend.producer(conn, tid, 'budget stopped')
    assert before == {'kind': 'unavailable', 'error': 'scope_authority_unavailable'}
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == count  # no synthetic guard
    task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    result = adopt(tid, capsys)
    assert result.get('adopted') is True, result
    assert result['session_id'] == 'chat'
    assert spend.producer(conn, tid, 'budget stopped again') == {'kind': 'chats'}
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == count
    assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == task
    row = conn.execute('SELECT delivery_metadata FROM kanban_notify_subs WHERE task_id=?', (tid,)).fetchone()
    assert json.loads(row[0])['reason'] == 'origin'
    assert adopt(tid, capsys)['adopted'] is True


@pytest.mark.parametrize('source', ['kanban', 'oneshot', 'cron', 'subagent', None])
def test_machine_or_unknown_session_refused(world, capsys, source):
    conn, tid, home = world
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('UPDATE sessions SET source=?,created_source=?', (source, source))
    result = adopt(tid, capsys)
    assert result['code'] == 'origin_interactive_required'
    assert json.loads(conn.execute('SELECT delivery_metadata FROM kanban_notify_subs').fetchone()[0]) == {'reason': 'shepherd'}


@pytest.mark.parametrize('key', ['HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'])
def test_worker_cannot_nominate_a_human_chat(world, capsys, monkeypatch, key):
    _, tid, _ = world
    monkeypatch.setenv(key, 'worker')
    assert adopt(tid, capsys)['code'] == 'origin_interactive_required'


@pytest.mark.parametrize('kind', ['subscription', 'creator'])
def test_another_chat_owner_refused(world, capsys, kind):
    conn, tid, _ = world
    if kind == 'subscription':
        add_notify_sub(conn, task_id=tid, platform='tui', chat_id='other', delivery_metadata={'reason': 'origin'})
    else:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id='other' WHERE id=?", (tid,))
    result = adopt(tid, capsys)
    assert result['code'] == 'origin_adoption_conflict'
    assert json.loads(conn.execute("SELECT delivery_metadata FROM kanban_notify_subs WHERE chat_id='chat'").fetchone()[0]) == {'reason': 'shepherd'}


def test_actual_parser_accepts_local_and_retains_courier_route():
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['kanban', 'origin-adopt', 't_existing', '--local'])
    assert args.task_id == 't_existing' and args.local and args.request_file is None
    courier = parser.parse_args(['kanban', 'origin-adopt', '--request-file', '-'])
    assert courier.request_file == '-' and not courier.local


def test_turn_identity_wins_over_process_environment(world, capsys, monkeypatch):
    from gateway.session_context import set_session_vars, clear_session_vars
    _, tid, _ = world
    monkeypatch.setenv('HERMES_SESSION_ID', 'wrong_chat')
    tokens = set_session_vars(session_id='chat', session_key='chat', source='desktop')
    try:
        assert adopt(tid, capsys)['session_id'] == 'chat'
    finally:
        clear_session_vars(tokens)


def test_machine_creation_source_wins_over_interactive_resume(world, capsys):
    _, tid, home = world
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE sessions SET source='desktop',created_source='oneshot'")
    assert adopt(tid, capsys)['code'] == 'origin_interactive_required'


def test_unreadable_state_fails_closed(world, capsys):
    _, tid, home = world
    (home / 'state.db').unlink()
    assert adopt(tid, capsys)['code'] == 'origin_interactive_required'


def test_existing_cursor_routing_and_mode_preserved(world, capsys):
    conn, tid, _ = world
    add_notify_sub(conn, task_id=tid, platform='tui', chat_id='chat', delivery_mode='notify+wake',
                   delivery_metadata={'route': 'keep'})
    before = dict(conn.execute('SELECT * FROM kanban_notify_subs').fetchone())
    assert adopt(tid, capsys)['adopted']
    after = dict(conn.execute('SELECT * FROM kanban_notify_subs').fetchone())
    metadata = json.loads(after.pop('delivery_metadata'))
    before.pop('delivery_metadata')
    assert after == before
    assert metadata == {'reason': 'origin', 'route': 'keep'}


def test_no_subscription_is_created_and_missing_task_refused(world, capsys):
    conn, tid, _ = world
    with kb.write_txn(conn):
        conn.execute('DELETE FROM kanban_notify_subs WHERE task_id=?', (tid,))
    assert adopt(tid, capsys)['adopted']
    assert adopt('t_missing', capsys)['code'] == 'origin_task_missing'


def test_conflicting_created_link_refused(world, capsys):
    conn, tid, _ = world
    from hermes_cli.kanban_chat_links import link_uid
    with kb.write_txn(conn):
        conn.execute('CREATE TABLE IF NOT EXISTS task_chat_links (id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT NOT NULL, '
                     'chat_gateway TEXT NOT NULL, chat_profile TEXT, chat_session TEXT NOT NULL, kind TEXT NOT NULL, '
                     'released INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)')
        conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES (?,?,?,?,?,'created',0,1)",
                     (link_uid(tid, 'remote', 'default', 'other', 'created'), tid, 'remote', 'default', 'other'))
    assert adopt(tid, capsys)['code'] == 'origin_adoption_conflict'

