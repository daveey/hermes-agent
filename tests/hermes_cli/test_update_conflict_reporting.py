"""Configured conflict routing never creates competing owners or leaks inventories."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import update_upstream_conflict as reporter
from hermes_cli.config import save_config, validate_config_structure
from hermes_cli.kanban_db_connect import connect_closing

OWNER = 't_0123456789abcdef'
REF = f'@task:default@hub/{OWNER}'
REPO = 'https://github.com/example/core.git'


@pytest.fixture
def routing(tmp_path, monkeypatch):
    from hermes_cli import kanban_capability_registry as registry
    monkeypatch.setattr(registry, 'self_gateway', lambda: 'hub')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    save_config({'platform_toolsets': {'cli': ['terminal', 'file']}, 'terminal': {'backend': 'local'}})
    with connect_closing() as conn:
        consumer = reporter._report(conn, checkout, 'a' * 40, ['old.txt'])
    from hermes_cli import update_conflict_reporting as routing
    monkeypatch.setattr(routing, '_repository', lambda cwd: REPO)
    key = 'upstream-merge-conflict:' + hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:24]
    owner = {'uid': OWNER, 'created_on': 'hub', 'created_by': 'upstream-merge', 'status': 'triage',
             'title': 'Resolve unattended upstream merge conflict', 'idempotency_key': key,
             'body': f'`hermes update` could not merge upstream into `{checkout}`. Nothing moved.\n',
             'version': 1, 'deleted_at': None}
    evidence = {'reporting_gateway': 'hub', 'board': 'default', 'table': 'tasks', 'uid': OWNER, 'head': 1,
                'state': 'live', 'tombstone': None, 'row': owner}
    monkeypatch.setattr(routing, '_owner_evidence', lambda board, tid: evidence)
    cfg = {'mode': 'pointer', 'owner': REF, 'repository': REPO, 'owner_checkout': str(checkout)}
    save_config({'update': {'upstream_conflict_reporting': cfg}})
    return checkout, consumer, cfg, evidence


def comments(conn, tid):
    return [r[0] for r in conn.execute('SELECT body FROM task_comments WHERE task_id=?', (tid,))]


def test_pointer_dedupes_changed_sha_and_preserves_consumer(routing):
    checkout, consumer, _, _ = routing
    with connect_closing() as conn:
        before = tuple(conn.execute('SELECT body,status,idempotency_key FROM tasks WHERE id=?', (consumer,)).fetchone())
        for sha in ('b' * 40, 'b' * 40, 'c' * 40):
            assert reporter._report(conn, checkout, sha, ['secret.txt']) == consumer
        notes = comments(conn, consumer)
        assert len(notes) == 2
        assert all(REF in note and 'secret.txt' not in note and 'Conflicting paths' not in note for note in notes)
        assert tuple(conn.execute('SELECT body,status,idempotency_key FROM tasks WHERE id=?', (consumer,)).fetchone()) == before
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_pointer_archived_consumer_and_other_checkout_never_create_owner(routing):
    checkout, consumer, _, _ = routing
    with connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (consumer,))
        conn.commit()
        for path in (checkout, checkout.parent / 'another'):
            assert reporter._report(conn, path, 'b' * 40, ['secret.txt']) == OWNER
        assert comments(conn, consumer) == []
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_inventory_targets_existing_owner_only(routing):
    checkout, consumer, cfg, _ = routing
    cfg['mode'] = 'inventory'
    # The canonical owner must also be this local board's exact existing row.
    with connect_closing() as conn:
        cfg['owner'] = f'@task:default@hub/{consumer}'
        from hermes_cli import update_conflict_reporting as routing
        routing_owner = routing._owner_evidence('hermes', OWNER)
        routing_owner['uid'] = routing_owner['row']['uid'] = consumer
        save_config({'update': {'upstream_conflict_reporting': cfg}})
        for sha in ('b' * 40, 'b' * 40, 'c' * 40):
            assert reporter._report(conn, checkout, sha, ['inventory.txt']) == consumer
        assert len(comments(conn, consumer)) == 2
        assert all('inventory.txt' in c for c in comments(conn, consumer))
        with pytest.raises(ValueError, match='inventory'):
            reporter._report(conn, checkout.parent / 'elsewhere', 'd' * 40, ['no.txt'])
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['missing', 'deleted', 'gateway', 'project', 'key', 'terminal', 'author', 'title', 'body', 'board',
                                    'uid', 'row_uid', 'table', 'head', 'version', 'tombstone', 'row_deleted'])
def test_bad_owner_fails_closed(routing, monkeypatch, fault):
    checkout, consumer, _, evidence = routing
    from hermes_cli import update_conflict_reporting as routing
    if fault == 'project':
        monkeypatch.setattr(routing, '_repository', lambda cwd: 'https://github.com/other/project.git')
    mutations = {'missing': lambda: evidence.update(row=None),
                 'deleted': lambda: evidence.update(state='deleted'),
                 'board': lambda: evidence.update(board='other'),
                 'uid': lambda: evidence.update(uid='t_00000000'),
                 'table': lambda: evidence.update(table='task_events'),
                 'head': lambda: evidence.update(head=None),
                 'tombstone': lambda: evidence.update(tombstone={})}
    if fault in mutations:
        mutations[fault]()
    fields = {'gateway': ('created_on', 'other'), 'key': ('idempotency_key', 'other'),
              'terminal': ('status', 'archived'), 'author': ('created_by', 'other'),
              'title': ('title', 'other'), 'body': ('body', 'other'),
              'row_uid': ('uid', 't_00000000'), 'version': ('version', 0), 'row_deleted': ('deleted_at', 1)}
    if fault in fields:
        field, value = fields[fault]
        evidence['row'][field] = value
    with connect_closing() as conn:
        with pytest.raises(ValueError):
            reporter._report(conn, checkout, 'b' * 40, ['secret.txt'])
        assert comments(conn, consumer) == []
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_unavailable_owner_fails_closed(routing, monkeypatch):
    checkout, consumer, _, _ = routing
    from hermes_cli import update_conflict_reporting as routing
    def unavailable(*args):
        raise TimeoutError('owner unavailable')
    monkeypatch.setattr(routing, '_owner_evidence', unavailable)
    with connect_closing() as conn:
        with pytest.raises(TimeoutError):
            reporter._report(conn, checkout, 'b' * 40, ['secret.txt'])
        assert comments(conn, consumer) == []


@pytest.mark.parametrize('config', [{}, {'mode': 'typo'}, {'mode': 'pointer', 'owner': 't_0123456789abcdef'}, 'pointer'])
def test_config_validation_and_runtime_refusal(routing, config):
    checkout, consumer, _, _ = routing
    assert any('upstream_conflict_reporting' in i.message for i in validate_config_structure(
        {'update': {'upstream_conflict_reporting': config}}))
    save_config({'update': {'upstream_conflict_reporting': config}})
    with connect_closing() as conn:
        with pytest.raises(ValueError):
            reporter._report(conn, checkout, 'b' * 40, ['secret.txt'])
        assert comments(conn, consumer) == []


def test_concurrent_pointers_are_serialized(routing):
    checkout, consumer, _, _ = routing
    def send(_):
        with connect_closing() as conn:
            return reporter._report(conn, checkout, 'b' * 40, ['secret.txt'])
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert set(pool.map(send, range(6))) == {consumer}
    with connect_closing() as conn:
        assert len(comments(conn, consumer)) == 1


def test_fresh_child_reads_config_and_authoritative_owner_without_mutating_source(routing, tmp_path, monkeypatch):
    checkout, consumer, cfg, evidence = routing
    for args in [('init', '-q'), ('remote', 'add', 'origin', REPO),
                 ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                  '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'fixture')]:
        subprocess.run(['git', *args], cwd=checkout, check=True, capture_output=True)
    before = subprocess.run(['git', 'show-ref'], cwd=checkout, capture_output=True, check=True).stdout
    requests = []

    class OwnerHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            raw = json.dumps(evidence).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), OwnerHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    key = tmp_path / 'gateway.key'
    key.write_text('fixture-key')
    save_config({'update': {'upstream_conflict_reporting': cfg}, 'kanban': {'sync': {
        'enabled': False, 'url': f'http://127.0.0.1:{server.server_port}', 'gateway': 'mac', 'key_file': str(key)}}})
    from tests.hermes_cli.test_update_upstream_conflict import _stale_config_effective
    _stale_config_effective(monkeypatch)
    try:
        assert reporter.report(checkout, 'b' * 40, ['secret.txt']) == consumer
        assert reporter.report(checkout, 'b' * 40, ['secret.txt']) == consumer
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert len(requests) == 2
    assert all(p == f'/v2/boards/default/evidence?table=tasks&uid={OWNER}' for p in requests)
    assert subprocess.run(['git', 'show-ref'], cwd=checkout, capture_output=True, check=True).stdout == before
    assert sorted(p.name for p in checkout.iterdir()) == ['.git']
    with connect_closing() as conn:
        assert len(comments(conn, consumer)) == 1
        assert 'secret.txt' not in comments(conn, consumer)[0]


def test_invalid_config_in_fresh_child_warns_and_never_creates_owner(routing, capsys):
    checkout, consumer, _, _ = routing
    save_config({'update': {'upstream_conflict_reporting': {'mode': 'pointer', 'owner': 'bad'}}})
    assert reporter.report(checkout, 'b' * 40, ['secret.txt']) is None
    assert 'could not file upstream merge conflict card' in capsys.readouterr().out
    with connect_closing() as conn:
        assert comments(conn, consumer) == []
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('origin', ['git@github.com:example/core.git', REPO, 'https://github.com/example/core'])
def test_repository_normalization(tmp_path, origin):
    from hermes_cli import update_conflict_reporting as routing
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', origin], cwd=tmp_path, check=True)
    assert routing._repository(tmp_path) == REPO


@pytest.mark.parametrize('broken', ['update: [', '- not-a-mapping'])
def test_malformed_file_never_recovers_into_legacy_creation(routing, broken, capsys):
    checkout, consumer, _, _ = routing
    from hermes_cli.config import get_config_path
    from ruamel.yaml.error import YAMLError
    from hermes_cli import update_conflict_reporting as routing
    get_config_path().write_text(broken)
    with pytest.raises((ValueError, TypeError, YAMLError)):
        routing.configured()
    assert reporter.report(checkout, 'b' * 40, ['secret.txt']) is None
    assert 'could not file upstream merge conflict card' in capsys.readouterr().out
    with connect_closing() as conn:
        assert comments(conn, consumer) == []
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_foreign_pickup_is_not_a_local_consumer(routing):
    checkout, consumer, _, _ = routing
    with connect_closing() as conn:
        conn.execute('ALTER TABLE tasks ADD COLUMN created_on TEXT')
        conn.execute('UPDATE tasks SET created_on=? WHERE id=?', ('foreign', consumer))
        conn.commit()
        assert reporter._report(conn, checkout, 'b' * 40, ['secret.txt']) == OWNER
        assert comments(conn, consumer) == []
        conn.execute('UPDATE tasks SET created_on=? WHERE id=?', ('hub', consumer))
        conn.commit()
        assert reporter._report(conn, checkout, 'b' * 40, ['secret.txt']) == consumer
        assert len(comments(conn, consumer)) == 1


@pytest.mark.parametrize('field,value', [('reporting_gateway', ''), ('reporting_gateway', 'mac')])
def test_inventory_requires_authenticated_owner_gateway(routing, field, value):
    checkout, _, cfg, evidence = routing
    cfg['mode'] = 'inventory'
    evidence[field] = value
    save_config({'update': {'upstream_conflict_reporting': cfg}})
    with connect_closing() as conn:
        with pytest.raises(ValueError):
            reporter._report(conn, checkout, 'b' * 40, ['secret.txt'])


def test_two_local_checkouts_retain_separate_pointer_incidents(routing):
    checkout, consumer, cfg, _ = routing
    other = checkout.parent / 'second-checkout'
    other.mkdir()
    save_config({'update': {'upstream_conflict_reporting': None}})
    with connect_closing() as conn:
        second = reporter._report(conn, other, 'a' * 40, ['other-old.txt'])
    save_config({'update': {'upstream_conflict_reporting': cfg}})
    with connect_closing() as conn:
        for _ in range(2):
            assert reporter._report(conn, checkout, 'b' * 40, ['secret.txt']) == consumer
            assert reporter._report(conn, other, 'b' * 40, ['secret.txt']) == second
        assert len(comments(conn, consumer)) == len(comments(conn, second)) == 1
        assert comments(conn, consumer) != comments(conn, second)
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
