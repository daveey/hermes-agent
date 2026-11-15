"""Worker discovery is routing only; the origin still authorizes every handoff."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_selfwork_outbound as outbound, kanban_origin
from tests.hermes_cli import test_kanban_selfwork_outbound as helpers
from tests.hermes_cli import test_kanban_selfwork_entry as entry

board = helpers.board
local = helpers.local
sender = helpers.sender


@pytest.fixture
def worker(sender, local, monkeypatch):
    from hermes_cli import kanban_db as kb
    tid = kb.create_task(local, title='creator')
    # Replica discovery deliberately differs from the executor profile/gateway.
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, 'sync_enabled', lambda: True)
    sync.install_or_remove(local, str(kb.kanban_db_path()))
    local.execute("UPDATE tasks SET created_on='creator-home', status='running', current_run_id=93 WHERE id=?", (tid,))
    local.execute("INSERT INTO task_runs(id,task_id,status,started_at,uid) VALUES(93,?,'running',1,'portable-run')", (tid,))
    local.commit()
    monkeypatch.setenv('HERMES_KANBAN_WORKER_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '93')
    monkeypatch.setenv('HERMES_ORIGIN_SESSION', '@session:chat-profile@chat-gateway/chat')
    monkeypatch.setattr(kanban_origin, '_replica_task_owners', lambda task, board: [
        ('creator-home', 'creator-profile'), ('executor', 'default')])
    return local, tid


@pytest.mark.parametrize('surface', ['cli', 'tool'])
def test_worker_frontdoors_forward_to_origin(worker, monkeypatch, surface):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, json.loads(kwargs['input'])))
        return SimpleNamespace(returncode=0, stdout=argv[argv.index('--id') + 1] + '\n', stderr='')
    monkeypatch.setattr(outbound.subprocess, 'run', run)
    result = entry.create(surface, entry.support.declaration())
    assert result == 0 if surface == 'cli' else result['code'] == 'self_work_queued', result
    argv, body = calls[0]
    assert argv[2:6] == ['chat-gateway', 'sessions', 'chat-profile/chat', 'selfwork-forward']
    assert body['creator'] == {'gateway': 'creator-home', 'profile': 'creator-profile',
                               'board': 'default', 'task_id': worker[1], 'run_uid': 'portable-run'}
    assert body['origin'] == {'gateway': 'chat-gateway', 'profile': 'chat-profile', 'session': 'chat'}
    assert body['create']['board'] == 'incidents'
    assert 'session_id' not in body['create']


@pytest.mark.parametrize('damage', ['wrong-task', 'ended', 'stale-run', 'missing-uid', 'ambiguous-owner', 'no-owner', 'no-origin'])
def test_worker_discovery_fails_without_transport(worker, monkeypatch, damage):
    conn, tid = worker
    updates = {
        'wrong-task': ("UPDATE task_runs SET task_id='t_other' WHERE id=93", ()),
        'ended': ('UPDATE task_runs SET ended_at=2 WHERE id=93', ()),
        'stale-run': ('UPDATE tasks SET current_run_id=94 WHERE id=?', (tid,)),
        'missing-uid': ('UPDATE task_runs SET uid=NULL WHERE id=93', ()),
    }
    if damage in updates:
        conn.execute(*updates[damage])
    elif damage in {'ambiguous-owner', 'no-owner'}:
        owners = [('creator-home', 'a'), ('creator-home', 'b')] if damage == 'ambiguous-owner' else []
        monkeypatch.setattr(kanban_origin, '_replica_task_owners', lambda *a: owners)
    else:
        monkeypatch.delenv('HERMES_ORIGIN_SESSION')
    conn.commit()
    monkeypatch.setattr(outbound.subprocess, 'run', lambda *a, **k: pytest.fail('queued invalid discovery'))
    with pytest.raises(ValueError):
        outbound.submit({'self_work': entry.support.declaration()})


@pytest.mark.parametrize('run', ['', '-1', '0', '９３', '1' * 100, '9223372036854775808'])
def test_invalid_run_is_a_typed_refusal(worker, monkeypatch, run):
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', run)
    with pytest.raises(ValueError, match='missing creator run'):
        outbound.prepare({'self_work': entry.support.declaration()})


def test_conflicting_worker_markers_are_refused(worker, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'other-task')
    with pytest.raises(ValueError, match='missing creator run'):
        outbound.prepare({'self_work': entry.support.declaration()})


def test_worker_idempotency_key_is_scoped_to_creator(worker, monkeypatch):
    args = {'self_work': entry.support.declaration(), 'idempotency_key': 'same'}
    first = outbound.prepare(args)[2]
    worker[0].execute("UPDATE task_runs SET uid='another-run' WHERE id=93")
    worker[0].commit()
    assert outbound.prepare(args)[2] != first
