"""Producer CLI → real receiver → durable owner receipt, without network writes."""
import argparse
import io
import json
import sqlite3

import pytest

from hermes_cli import kanban, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_selfwork_producer as producer
from hermes_cli.kanban_parser import build_parser
from tests.hermes_cli import test_kanban_selfwork_nochat as support

world = support.world
request_envelope = support.request_envelope


def invoke(operation, value, monkeypatch, capsys):
    parser = build_parser(argparse.ArgumentParser().add_subparsers())
    args = parser.parse_args(['selfwork-producer', '--operation', operation])
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(value)))
    assert kanban.kanban_command(args) == 0
    return json.loads(capsys.readouterr().out)


def create_args(key='page1'):
    return {'title': 'CI flaky intake', 'body': 'immutable manifest page ' + key,
            'idempotency_key': key, 'assignee': 'default',
            'self_work': {'version': 1, 'repository': 'hermes-core', 'area': 'ci', 'kind': 'ci-flake'}}


def test_composed_cli_owner_create_append_and_replay(request_envelope, tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban_selfwork_outbound as outbound

    monkeypatch.delenv('HERMES_KANBAN_WORKER_TASK', raising=False)
    hub = tmp_path / 'hub'
    hub.mkdir(exist_ok=True)
    db = sqlite3.connect(hub / 'hub.db')
    db.executescript("CREATE TABLE meta(k TEXT,v TEXT); INSERT INTO meta VALUES('gateway','owner'); "
                     'CREATE TABLE sent(id TEXT PRIMARY KEY,body TEXT,state TEXT,result TEXT)')
    db.commit()
    requests = []

    def transport(home, body, rid):
        request = {'id': rid, 'from': 'gw:owner', 'to_gw': home['gateway'], 'coll': 'tasks',
                   'action': 'selfwork-create', 'key': home['profile'] + '/' + home['board'], 'args': body}
        result = support.invoke(request, monkeypatch, capsys)
        assert result.get('handoff_complete') is True, result
        requests.append(result)
        with db:
            db.execute('INSERT OR REPLACE INTO sent VALUES(?,?,?,?)',
                       (rid, json.dumps({k: v for k, v in request.items() if k != 'from'}), 'done', json.dumps(result)))
        return {'code': 'self_work_queued'}

    monkeypatch.setattr(outbound, 'send', transport)
    try:
        assert invoke('capabilities', None, monkeypatch, capsys) == {'self_work_producer': 1}
        first = invoke('prepare', create_args(), monkeypatch, capsys)
        assert not requests
        result = invoke('advance', first, monkeypatch, capsys)
        assert result['status'] == 'completed', (result, requests)
        assert result['disposition'] == 'create'
        second = invoke('prepare', create_args('page2'), monkeypatch, capsys)
        appended = invoke('advance', second, monkeypatch, capsys)
        assert appended['disposition'] == 'append', appended
        assert appended['owner'] == result['owner']
        assert invoke('advance', second, monkeypatch, capsys) == appended
        # Lost courier cache after commit replays the same intake journal receipt.
        with db:
            db.execute('DELETE FROM sent')
        assert invoke('advance', second, monkeypatch, capsys) == appended
        assert requests[-1]['replayed'] is True
        with kbc.connect_closing() as conn:
            tasks = kb.list_tasks(conn)
            assert len(tasks) == 1
            assert tasks[0].idempotency_key == 'page1'
            assert 'immutable manifest page page1' in tasks[0].body
            notes = kb.list_comments(conn, tasks[0].id)
            assert len(notes) == 1
            assert 'immutable manifest page page2' in notes[0].body
    finally:
        db.close()


@pytest.mark.parametrize('field,value', [('self_work', None), ('title', ''), ('body', None), ('idempotency_key', ''),
                                       ('assignee', 1), ('session_id', 'chat')])
def test_invalid_prepare_does_not_submit(request_envelope, monkeypatch, capsys, field, value):
    from hermes_cli import kanban_selfwork_outbound as outbound
    monkeypatch.delenv('HERMES_KANBAN_WORKER_TASK', raising=False)
    monkeypatch.setattr(outbound, 'send', lambda *a: pytest.fail('invalid prepare submitted'))
    result = invoke('prepare', {**create_args(), field: value}, monkeypatch, capsys)
    assert result['status'] == 'pending'


@pytest.mark.parametrize('variable', ['HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK'])
def test_worker_cannot_use_headless_prepare(request_envelope, monkeypatch, capsys, variable):
    monkeypatch.setenv(variable, 't_worker')
    assert invoke('prepare', create_args(), monkeypatch, capsys)['status'] == 'pending'


@pytest.mark.parametrize('field,value', [('version', True), ('version', 2), ('sender', '../foreign'),
                                       ('home', []), ('body', []), ('request_id', 'sw-other')])
def test_invalid_snapshot_never_sends(request_envelope, monkeypatch, capsys, field, value):
    monkeypatch.delenv('HERMES_KANBAN_WORKER_TASK', raising=False)
    frozen = invoke('prepare', create_args(), monkeypatch, capsys)
    monkeypatch.setattr(producer, 'read_receipt', lambda *a: pytest.fail('invalid snapshot read'))
    assert invoke('advance', {**frozen, field: value}, monkeypatch, capsys)['status'] == 'pending'
