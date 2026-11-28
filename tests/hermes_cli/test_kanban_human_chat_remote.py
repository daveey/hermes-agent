"""Worker-a1 DoorDash ask: no execution row on hub, only a shepherd link. [daveey]"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_human_chat as human, kanban_human_chat_remote as remote
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli.test_kanban_human_chat import IPHONE_REQUEST, HUMAN_CHAT

pytest_plugins = ('tests.hermes_cli.test_kanban_human_chat',)


@pytest.fixture
def worker_shadow(conn, home, monkeypatch):
    path = home / 'worker-shadow.db'
    with kbc.connect_closing(path) as shadow:
        client.ensure_identity(shadow, 'default', 'worker-a1')
        tid = kb.create_task(shadow, title='Reconcile DoorDash renewal inquiry ownership', assignee='default')
        with kb.write_txn(shadow):
            shadow.execute("UPDATE tasks SET session_id=NULL,created_on='worker-a1' WHERE id=?", (tid,))
            shadow.execute('DELETE FROM task_chat_links WHERE task_id=?', (tid,))
            shadow.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,"
                           "released,created_at) VALUES (?,?, 'hub','default',?,'shepherd',0,1)",
                           (links.link_uid(tid, 'hub', 'default', HUMAN_CHAT, 'shepherd'), tid, HUMAN_CHAT))
        assert kb.block_task(shadow, tid, kind='needs_input', human_request=IPHONE_REQUEST)
    monkeypatch.setattr(client, 'shadow_path', lambda board: path)
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, '_replica_task_owners', lambda tid, board: [('worker-a1', 'default')])
    return tid, path


def test_home_sweep_finds_worker_ask_without_local_task_and_queues_not_claims_link(conn, worker_shadow, monkeypatch):
    tid, path = worker_shadow
    assert not conn.execute('SELECT 1 FROM tasks WHERE id=?', (tid,)).fetchone()
    calls = []
    monkeypatch.setattr(remote, 'enqueue', lambda card, sid: calls.append((card, sid)) or {**card, 'session': sid})
    dry = human.sweep(conn, apply=False)
    assert [c['task'] for c in dry['remote']['missing']] == [tid]
    assert not calls
    out = human.sweep(conn)
    assert out['linked'] == []
    assert [c['task'] for c in out['remote']['pending']] == [tid]
    assert len(calls) == 1 and calls[0][1] == human.needs_you_session()
    assert not conn.execute('SELECT 1 FROM tasks WHERE id=?', (tid,)).fetchone()
    # Exact owner-side accepted link appears in the shadow; next sweep is idempotent.
    with kbc.connect_closing(path) as shadow, kb.write_txn(shadow):
        shadow.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,"
                       "released,created_at) VALUES (?,?,'hub','default',?,'created',0,1)",
                       (links.link_uid(tid, 'hub', 'default', human.needs_you_session(), 'created'), tid,
                        human.needs_you_session()))
    assert remote.sweep(conn, apply=True)['missing'] == []
    assert len(calls) == 1


@pytest.mark.parametrize('status,human_request,owner', [('done', IPHONE_REQUEST, 'worker-a1'),
                         ('blocked', '{}', 'worker-a1'), ('blocked', IPHONE_REQUEST, 'hub')])
def test_closed_invalid_and_home_rows_are_not_adopted(conn, worker_shadow, status, human_request, owner):
    tid, path = worker_shadow
    with kbc.connect_closing(path) as shadow, kb.write_txn(shadow):
        shadow.execute('UPDATE tasks SET status=?,human_request=?,created_on=? WHERE id=?',
                       (status, json.dumps(human_request) if isinstance(human_request, dict) else human_request, owner, tid))
    assert remote.candidates(conn) == []


def test_nonhome_only_filter_and_failed_discovery(conn, worker_shadow, monkeypatch):
    assert remote.sweep(conn, apply=True, only=['not-this-card'])['missing'] == []
    monkeypatch.setattr(human, 'home_gateway', lambda: 'elsewhere')
    assert remote.candidates(conn) == []
    monkeypatch.setattr(human, 'home_gateway', lambda: 'hub')
    monkeypatch.setattr(remote, 'candidates', lambda conn: (_ for _ in ()).throw(OSError('unreadable')))
    assert remote.sweep(conn, apply=True)['failed'] == [{'source': 'replica_discovery'}]


def test_enqueue_is_exact_idempotent_and_never_writes_task(conn, monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        assert kw['timeout'] == 20 and kw['check'] is False
        assert kw['encoding'] == 'utf-8' and kw['errors'] == 'replace'
        assert kw['stdin'] == remote.subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(remote.subprocess, 'run', run)
    card = {'gateway': 'worker-a1', 'profile': 'default', 'board': 'default', 'task': 't_01a219b2895e8640'}
    first = remote.enqueue(card, 'kanban_owner_needs_you')
    assert first == remote.enqueue(card, 'kanban_owner_needs_you')
    assert calls[0] == calls[1]
    assert calls[0][:4] == ['hermes-hub', 'adopt-task', 'worker-a1', 'default/default/t_01a219b2895e8640']
    assert remote.enqueue(card, 'kanban_owner_needs_you_2')['request_id'] != first['request_id']
    monkeypatch.setattr(remote.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=2, stderr='refused'))
    with pytest.raises(RuntimeError, match='refused'):
        remote.enqueue(card, 'kanban_owner_needs_you')


def test_enqueue_drain_refusal_prevents_courier_spawn(monkeypatch):
    from hermes_cli import worker_drain_fence as fence
    calls = []
    monkeypatch.setattr(remote.subprocess, 'run', lambda *a, **kw: calls.append(a))
    def refuse(purpose):
        assert purpose == 'kanban.human_chat_remote'
        raise fence.DrainRefused('draining')
    monkeypatch.setattr(fence, 'admit', refuse)
    with pytest.raises(fence.DrainRefused, match='draining'):
        remote.enqueue({'gateway': 'worker-a1'}, human.needs_you_session())
    assert calls == []


def test_profile_routes_are_preserved_and_ambiguous_routes_fail_closed(conn, worker_shadow, monkeypatch):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, '_replica_task_owners', lambda tid, board: [('worker-a1', 'bb-dev')])
    card = remote.candidates(conn)[0]
    assert card['profile'] == 'bb-dev'
    calls = []
    monkeypatch.setattr(remote.subprocess, 'run', lambda argv, **kw: calls.append(argv) or
                        SimpleNamespace(returncode=0, stderr=''))
    remote.enqueue(card, human.needs_you_session())
    assert calls[0][3].startswith('bb-dev/default/')
    monkeypatch.setattr(kanban_origin, '_replica_task_owners', lambda tid, board:
                        [('worker-a1', 'default'), ('worker-a1', 'bb-dev')])
    assert remote.sweep(conn, apply=True)['failed'] == [{'source': 'replica_discovery'}]
    assert len(calls) == 1


@pytest.mark.parametrize('kind,released,expected', [('created', 0, 0), ('shepherd', 0, 1), ('created', 1, 1)])
def test_foreign_hash_ownership_requires_unreleased_created_link(conn, worker_shadow, kind, released, expected):
    tid, path = worker_shadow
    sid = 'local-' + 'a' * 64
    with kbc.connect_closing(path) as shadow, kb.write_txn(shadow):
        shadow.execute('UPDATE task_chat_links SET chat_gateway=?,chat_session=?,kind=?,released=? WHERE task_id=?',
                       ('macbook', sid, kind, released, tid))
    assert len(remote.candidates(conn)) == expected


def test_command_reports_remote_only_gap_and_pending_is_not_success(conn, worker_shadow, monkeypatch, capsys):
    monkeypatch.setattr(remote, 'enqueue', lambda card, sid: {**card, 'session': sid, 'request_id': 'pending-id'})
    assert human.command(SimpleNamespace(apply=False, json=False)) == 0
    assert 'remote asks have no human chat' in capsys.readouterr().out
    assert human.command(SimpleNamespace(apply=True, json=True)) == 1
    assert json.loads(capsys.readouterr().out)['remote']['pending']
