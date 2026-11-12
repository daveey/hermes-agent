"""All-origin orchestration via fake SSH; no live migration or credentials."""
import hashlib
import io
import json
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_priority_cli as cli
from hermes_cli import kanban_priority_executor as executor
from hermes_cli import kanban_priority_runner as runner
from tests.hermes_cli import test_kanban_priority_cli as cli_helpers
from tests.hermes_cli import test_kanban_priority_executor as helpers
from tests.hermes_cli import test_kanban_sync_client as sync_helpers

REAL_SUBPROCESS_RUN = subprocess.run
kanban_home = sync_helpers.kanban_home
conn = sync_helpers.live


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from agent import delegation_context
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setattr(delegation_context, 'is_delegated_child_process_context', lambda: False)
    origins = {'a': 1, 'b': 1, 'c': 1}
    hosts = {o: {'host': f'{o}.example.com', 'argv': ['hermes', 'kanban', '--board', 'test', 'supervisor-requests'],
                 'task': f'task-{o}', 'backup': str(tmp_path / f'{o}.json')} for o in origins}
    args = cli_helpers.args('--priority-operation', 'migrate', '--writer', 'a',
                            '--host-map', json.dumps(hosts), '--allow-legacy-rollback')
    data = '{ "note": "caf\u00e9", "rows": [] }\r\n'.encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    events = []
    rig_ns = SimpleNamespace(staged_origins=origins, manifest={
        'board_id': 'board', 'rows': [{'id': f't-{o}', 'origin': o, 'old': 1, 'new': 2} for o in origins]})
    def receipt(o):
        return {'writer': o, 'manifest_sha256': digest, 'attachment_id': 1,
                'origins': origins, 'backup': hosts[o]['backup']}
    def capture(_conn, _args):
        events.append(('capture', _args.writer))
        return rig_ns.manifest
    def prepare(_conn, _args, *, manifest=None):
        events.append(('prepare', 'a'))
        assert _args.priority_operation == 'prepare'
        assert _args.task == 'task-a'
        assert manifest is rig_ns.manifest  # stages the coverage-checked capture, no re-capture
        (tmp_path / 'a.json').write_bytes(data)
        return {**receipt('a'), 'origins': dict(rig_ns.staged_origins)}
    def apply(_conn, attachment, dg, *, writer, undo, allow_legacy_rollback):
        assert attachment == 1 and dg == digest
        assert allow_legacy_rollback
        events.append(('restore' if undo else 'apply', writer))
        return receipt(writer)
    def publish(_conn, writer):
        events.append(('publish', writer))
        return {'writer': writer, 'pending': 0, 'rejected': 0}
    def verify(_conn, _args, *, undo):
        assert _args.manifest == hosts['a']['backup'] and _args.digest == digest
        events.append(('verify-restore' if undo else 'verify', 'a'))
        return {'verified': 3}
    def transport(cmd, **kwargs):
        assert cmd[:5] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20']
        assert cmd[-3] == '--'
        words = shlex.split(cmd[-1])
        assert words[:5] == hosts['b']['argv']
        action = words[words.index('--priority-operation') + 1]
        origin = words[words.index('--writer') + 1]
        assert cmd[-2] == hosts[origin]['host']
        assert kwargs['encoding'] == 'utf-8' and kwargs['text']
        assert kwargs['env'].get('RUNNER_TEST_SENTINEL') == 'retained'
        if action == 'stage':
            assert kwargs['input'].encode('utf-8') == data
            assert words[words.index('--manifest') + 1] == '-'
            assert words[words.index('--backup') + 1] == hosts[origin]['backup']
        else:
            assert kwargs['input'] is None
        if action in ('apply', 'restore'):
            assert words[words.index('--digest') + 1] == digest
            assert words[words.index('--attachment-id') + 1] == '1'
        if action == 'restore':
            assert '--allow-legacy-rollback' in words
        events.append((action, origin))
        output = {'writer': origin, 'pending': 0, 'rejected': 0} if action == 'publish' else receipt(origin)
        return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr='')
    monkeypatch.setenv('RUNNER_TEST_SENTINEL', 'retained')
    monkeypatch.setattr(cli, '_capture', capture)
    monkeypatch.setattr(cli, '_prepare_stage', prepare)
    monkeypatch.setattr(cli, '_publish', publish)
    monkeypatch.setattr(cli, '_verify', verify)
    monkeypatch.setattr(executor, 'apply_attached', apply)
    monkeypatch.setattr(runner.subprocess, 'run', transport)
    rig_ns.__dict__.update(args=args, hosts=hosts, data=data, events=events, transport=transport, tmp_path=tmp_path)
    return rig_ns


def _set_host_map(rig, hosts):
    rig.args.host_map = json.dumps(hosts)


def _consumed_nothing(rig):
    assert not any(e[0] in ('prepare', 'stage', 'apply', 'restore', 'publish') for e in rig.events)
    assert not any(Path(entry['backup']).exists() for entry in rig.hosts.values())


def test_all_stages_before_apply_and_actual_command_wiring(rig, capsys):
    assert cli.run(None, rig.args) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report['staged']) == {'a', 'b', 'c'}
    assert report['attempted'] == ['a', 'b', 'c']
    assert set(report['applied']) == {'a', 'b', 'c'}
    assert report['canonical'] == {'verified': 3}
    assert rig.events == [('capture', 'a'), ('prepare', 'a'), ('stage', 'b'), ('stage', 'c'),
                          ('apply', 'a'), ('apply', 'b'), ('apply', 'c'),
                          ('publish', 'a'), ('publish', 'b'), ('publish', 'c'), ('verify', 'a')]


@pytest.mark.parametrize('bad', ['digest', 'writer', 'origins', 'attachment'])
def test_stage_mismatch_never_writes_priority(rig, monkeypatch, bad):
    def transport(cmd, **kwargs):
        result = rig.transport(cmd, **kwargs)
        receipt = json.loads(result.stdout)
        receipt[{'digest': 'manifest_sha256', 'writer': 'writer', 'origins': 'origins',
                 'attachment': 'attachment_id'}[bad]] = {'origins': {'a': 1}, 'attachment': True}.get(bad, 'wrong')
        result.stdout = json.dumps(receipt)
        return result
    monkeypatch.setattr(runner.subprocess, 'run', transport)
    code, report = runner.run(None, rig.args)
    assert code != 0 and report['errors']
    assert not report['applied']
    assert not any(e[0] in ('apply', 'restore', 'publish') for e in rig.events)


@pytest.mark.parametrize('restore_refuses', [False, True])
def test_later_apply_refusal_restores_attempted_origins_nonzero(rig, monkeypatch, restore_refuses):
    def transport(cmd, **kwargs):
        words = shlex.split(cmd[-1])
        if 'apply' in words and '--writer c' in cmd[-1]:
            return SimpleNamespace(returncode=1, stdout='', stderr='changed priority')
        if restore_refuses and 'restore' in words:
            return SimpleNamespace(returncode=1, stdout='', stderr='restore CAS refused')
        return rig.transport(cmd, **kwargs)
    monkeypatch.setattr(runner.subprocess, 'run', transport)
    code, report = runner.run(None, rig.args)
    assert code != 0
    assert set(report['applied']) == {'a', 'b'}
    assert report['attempted'] == ['a', 'b', 'c']
    assert set(report['restored']) == ({'a'} if restore_refuses else {'a', 'b', 'c'})
    assert ('restore', 'a') in rig.events
    assert ('apply', 'c') not in rig.events
    assert report['errors']
    if restore_refuses:
        assert any('guarded restore refused' in s for s in report['errors'])
        assert any('manual inspection required' in s for s in report['errors'])
        assert 'canonical_restore' not in report


@pytest.mark.parametrize('failure', ['timeout', 'parse', 'receipt'])
def test_apply_committed_without_acknowledgement_is_guardedly_restored(rig, monkeypatch, failure):
    committed = set()

    def transport(cmd, **kwargs):
        words = shlex.split(cmd[-1])
        action = words[words.index('--priority-operation') + 1]
        origin = words[words.index('--writer') + 1]
        result = rig.transport(cmd, **kwargs)
        if action == 'apply':
            committed.add(origin)
            if origin == 'b':
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(cmd, 300)
                result.stdout = ('not JSON' if failure == 'parse' else
                                 json.dumps({'writer': origin, 'manifest_sha256': 'wrong'}))
        elif action == 'restore':
            committed.discard(origin)
        return result

    monkeypatch.setattr(runner.subprocess, 'run', transport)
    code, report = runner.run(None, rig.args)
    assert code == 1 and report['errors']
    assert committed == set()
    assert report['attempted'] == ['a', 'b']
    assert set(report['applied']) == {'a'}
    assert set(report['restored']) == {'a', 'b'}
    assert [event for event in rig.events if event[0] == 'restore'] == [('restore', 'b'), ('restore', 'a')]
    assert ('apply', 'c') not in rig.events
    assert report['canonical_restore'] == {'verified': 3}


@pytest.mark.parametrize('delegated', [False, True])
def test_lineage_rejected_before_file_stdin_or_ssh(monkeypatch, delegated):
    from agent import delegation_context
    monkeypatch.setattr(delegation_context, 'is_delegated_child_process_context', lambda: delegated)
    if not delegated:
        monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    def forbidden(*_a, **_kw):
        pytest.fail('lineage fence reached protected I/O')
    monkeypatch.setattr(runner.subprocess, 'run', forbidden)
    monkeypatch.setattr(cli, '_manifest_bytes', forbidden)
    args = cli_helpers.args('--priority-operation', 'migrate', '--writer', 'a', '--host-map-file', '/missing')
    with pytest.raises(PermissionError):
        runner.run(None, args)
    with pytest.raises(PermissionError):
        runner.ssh({}, [], b'payload')
    args.priority_operation = 'stage'
    args.task, args.backup, args.manifest = 'task', '/missing', '-'
    assert cli.run(None, args) != 0


def test_stage_stdin_keeps_exact_original_bytes(conn, tmp_path, monkeypatch, capsys):
    ids, _, _ = helpers.setup_plan(conn)
    rows = [dict(r) for r in conn.execute('SELECT id,priority,created_on,status FROM tasks')]
    from hermes_cli.kanban_priority_migration import plan_global
    data = (json.dumps(plan_global(rows, board_id=sync_helpers.BOARD, exceptions=[]), indent=3) + '\r\n').encode()
    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=io.BytesIO(data)))
    backup = tmp_path / 'stdin.json'
    args = cli_helpers.args('--priority-operation', 'stage', '--writer', sync_helpers.ME,
                            '--manifest', '-', '--backup', str(backup), '--task', ids[0])
    assert cli.run(conn, args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert backup.read_bytes() == data
    assert receipt['manifest_sha256'] == hashlib.sha256(data).hexdigest()


def test_host_map_file_and_coverage(rig, tmp_path):
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps(rig.hosts), encoding='utf-8')
    rig.args.host_map, rig.args.host_map_file = None, str(path)
    assert runner.load_hosts(rig.args) == rig.hosts
    hosts = dict(rig.hosts)
    del hosts['c']
    path.write_text(json.dumps(hosts), encoding='utf-8')
    code, report = runner.run(None, rig.args)
    assert code != 0 and report['errors'] == ['host map missing manifest origins: c']
    assert report['missing'] == ['c'] and report['extra'] == []
    assert not report['applied'] and not report['staged']


@pytest.mark.parametrize('drop, add', [(['c'], []), (['b', 'c'], ['z'])])
def test_coverage_mismatch_refuses_before_any_backup_or_attachment(rig, drop, add):
    """Run 1 (op bdc414c0) staged the local backup and attachment, then refused on
    coverage. Missing origins must refuse after the read-only capture only."""
    hosts = {o: e for o, e in rig.hosts.items() if o not in drop}
    for origin in add:
        hosts[origin] = {**rig.hosts['a'], 'host': f'{origin}.example.com',
                         'backup': str(rig.tmp_path / f'{origin}.json')}
    _set_host_map(rig, hosts)
    rig.manifest['rows'].append({'id': 't-a2', 'origin': 'a', 'old': 3, 'new': 4})
    code, report = runner.run(None, rig.args)
    assert code == 1
    assert report['origins'] == {'a': 2, 'b': 1, 'c': 1}
    assert report['missing'] == sorted(drop) and report['extra'] == add
    assert report['staged'] == {} and report['attempted'] == []
    assert rig.events == [('capture', 'a')]
    _consumed_nothing(rig)


def test_superset_map_skips_origins_without_rows(rig):
    hosts = dict(rig.hosts, drained={**rig.hosts['b'], 'host': 'drained.example.com',
                                     'backup': str(rig.tmp_path / 'drained.json')})
    _set_host_map(rig, hosts)
    code, report = runner.run(None, rig.args)
    assert code == 0, report['errors']
    assert report['skipped'] == ['drained'] and report['extra'] == ['drained']
    assert report['missing'] == []
    assert set(report['staged']) == set(report['applied']) == {'a', 'b', 'c'}
    assert not any(origin == 'drained' for _action, origin in rig.events)
    assert not Path(hosts['drained']['backup']).exists()


def test_writer_without_rows_refuses_before_staging(rig):
    hosts = dict(rig.hosts, w={**rig.hosts['a'], 'backup': str(rig.tmp_path / 'w.json')})
    _set_host_map(rig, hosts)
    rig.args.writer = 'w'
    code, report = runner.run(None, rig.args)
    assert code == 1 and report['extra'] == ['w']
    assert any('local writer owns no manifest rows' in e for e in report['errors'])
    assert rig.events == [('capture', 'w')]
    _consumed_nothing(rig)


def test_writer_missing_from_map_refuses_before_staging(rig):
    _set_host_map(rig, {o: e for o, e in rig.hosts.items() if o != 'a'})
    code, report = runner.run(None, rig.args)
    assert code == 1 and report['missing'] == ['a']
    assert 'local writer missing from host map' in report['errors']
    _consumed_nothing(rig)


def test_staged_origins_must_equal_checked_capture(rig):
    rig.staged_origins = {'a': 1, 'b': 1}
    code, report = runner.run(None, rig.args)
    assert code == 1 and 'coverage-checked capture' in report['errors'][0]
    assert not any(e[0] in ('stage', 'apply') for e in rig.events)


def _origins_args(rig, *extra):
    return cli_helpers.args('--priority-operation', 'origins', '--writer', 'a',
                            '--host-map', rig.args.host_map, *extra)


@pytest.mark.parametrize('drop, add, code', [([], [], 0), (['c'], [], 1), ([], ['z'], 0), (['b'], ['z'], 1)])
def test_origins_check_is_read_only_and_reports_coverage(rig, capsys, drop, add, code):
    hosts = {o: e for o, e in rig.hosts.items() if o not in drop}
    for origin in add:
        hosts[origin] = {**rig.hosts['a'], 'backup': str(rig.tmp_path / f'{origin}.json')}
    _set_host_map(rig, hosts)
    assert cli.run(None, _origins_args(rig)) == code
    report = json.loads(capsys.readouterr().out)
    assert report['origins'] == {'a': 1, 'b': 1, 'c': 1}
    assert report['missing'] == drop and report['extra'] == add
    assert bool(report['errors']) == bool(code)
    assert rig.events == [('capture', 'a')]
    _consumed_nothing(rig)


@pytest.mark.parametrize('flag', [['--backup', '/x.json'], ['--task', 't'], ['--manifest', '-'],
                                  ['--digest', 'd'], ['--attachment-id', '1'], ['--allow-legacy-rollback']])
def test_origins_check_refuses_write_flags_before_capture(rig, flag):
    assert cli.run(None, _origins_args(rig, *flag)) != 0
    assert rig.events == []


@pytest.mark.parametrize('delegated', [False, True])
def test_origins_check_keeps_operator_fence(rig, monkeypatch, delegated):
    from agent import delegation_context
    monkeypatch.setattr(delegation_context, 'is_delegated_child_process_context', lambda: delegated)
    if not delegated:
        monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    assert cli.run(None, _origins_args(rig)) != 0
    with pytest.raises(PermissionError):
        runner.check_host_map(None, _origins_args(rig))
    assert rig.events == []


@pytest.mark.parametrize('operation', ['origins', 'migrate'])
def test_real_capture_path_writes_nothing_on_coverage_refusal(conn, monkeypatch, tmp_path, operation):
    """Un-faked _capture/_prepare_stage: an uncovered origin refuses with no backup file,
    no attachment and no priority change, for both the check and migrate itself."""
    ids, _, _ = helpers.setup_plan(conn)
    before = [tuple(r) for r in conn.execute('SELECT id, priority FROM tasks ORDER BY id')]
    from hermes_cli.kanban_priority_migration import plan_global
    inventory = [dict(r) for r in conn.execute('SELECT id,priority,created_on,status FROM tasks')]
    inventory.append({'id': 'remote', 'priority': 5, 'created_on': 'worker-9', 'status': 'ready'})
    calls = []
    def capture(hub, *, board_id, exceptions):
        calls.append((hub, board_id, exceptions))
        return plan_global(inventory, board_id=board_id, exceptions=exceptions)
    from hermes_cli import kanban_priority_prepare as prepare
    from hermes_cli import kanban_sync_client as sync
    monkeypatch.setattr(prepare, 'capture', capture)
    monkeypatch.setattr(sync, 'load_identity', lambda: SimpleNamespace(gateway=sync_helpers.ME))
    monkeypatch.setattr(sync, 'Hub', lambda identity: 'fake-hub')
    def forbidden(*_a, **_kw):
        pytest.fail('coverage refusal reached SSH')
    monkeypatch.setattr(runner.subprocess, 'run', forbidden)
    hosts = {sync_helpers.ME: {'host': 'me.example.com', 'argv': ['hermes'], 'task': ids[0],
                               'backup': str(tmp_path / 'never.json')}}
    args = cli_helpers.args('--priority-operation', operation, '--writer', sync_helpers.ME,
                            '--host-map', json.dumps(hosts))
    attachments_before = conn.execute('SELECT COUNT(*) FROM task_attachments').fetchone()[0]
    code, report = (runner.check_host_map if operation == 'origins' else runner.run)(conn, args)
    assert not (tmp_path / 'never.json').exists()  # run 1 consumed this path before refusing
    assert conn.execute('SELECT COUNT(*) FROM task_attachments').fetchone()[0] == attachments_before
    assert code == 1 and 'worker-9' in report['missing'] and sync_helpers.ME not in report['missing']
    assert report['origins']['worker-9'] == 1 and report['origins'][sync_helpers.ME] >= 1
    assert calls == [('fake-hub', sync_helpers.BOARD, [])]
    assert [tuple(r) for r in conn.execute('SELECT id, priority FROM tasks ORDER BY id')] == before


def test_publish_uses_supported_push_without_pull(conn, monkeypatch):
    from hermes_cli import kanban_sync_client as sync
    helpers.setup_plan(conn)
    monkeypatch.setattr(sync, 'load_identity', lambda: SimpleNamespace(gateway=sync_helpers.ME))
    monkeypatch.setattr(sync, 'Hub', lambda identity: 'fake-hub')
    calls = []
    def push(connection, hub, board, writer, *, primary):
        calls.append((connection, hub, board, writer, primary))
        return sync.PushResult()
    monkeypatch.setattr(sync, 'push', push)
    result = cli._publish(conn, sync_helpers.ME)
    assert result['pending'] == result['rejected'] == 0
    assert calls == [(conn, 'fake-hub', sync_helpers.BOARD, sync_helpers.ME, True)]


def test_canonical_failure_returns_nonzero_keeps_receipts(rig, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError('canonical mismatch')
    monkeypatch.setattr(cli, '_verify', refuse)
    code, report = runner.run(None, rig.args)
    assert code != 0 and len(report['applied']) == 3
    assert 'canonical mismatch' in report['errors'][0]
    assert not report['restored']
    assert not any(event[0] == 'restore' for event in rig.events)


def test_real_subprocess_fake_ssh_preserves_bytes_and_argv(rig, tmp_path, monkeypatch):
    saved = tmp_path / 'received.bin'
    argv_file = tmp_path / 'argv.json'
    executable = tmp_path / 'ssh'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import sys, json\nfrom pathlib import Path\n'
        f'Path({str(saved)!r}).write_bytes(sys.stdin.buffer.read())\n'
        f'Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]))\n'
        'print(json.dumps({"transport": "ok"}))\n', encoding='utf-8')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(runner.subprocess, 'run', REAL_SUBPROCESS_RUN)
    entry = {'host': 'operator@fake.example.com', 'argv': ['/command with spaces/hermes', 'kanban']}
    words = ['--task', "task ' quoted", '--manifest', '-']
    assert runner.ssh(entry, words, rig.data) == {'transport': 'ok'}
    assert saved.read_bytes() == rig.data
    argv = json.loads(argv_file.read_text(encoding='utf-8'))
    assert argv[:4] == ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20']
    assert argv[-3:-1] == ['--', entry['host']]
    assert shlex.split(argv[-1]) == entry['argv'] + words


@pytest.mark.parametrize('kind', ['worker', 'delegated'])
def test_remote_stage_stdin_fence_is_before_read(conn, monkeypatch, kind):
    from agent import delegation_context
    monkeypatch.setattr(delegation_context, 'is_delegated_child_process_context', lambda: kind == 'delegated')
    if kind == 'worker':
        monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    class NeverRead:
        def read(self):
            pytest.fail('unauthorized remote stdin read')
    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=NeverRead()))
    args = cli_helpers.args('--priority-operation', 'stage', '--writer', 'a',
                            '--task', 'task', '--backup', '/fresh', '--manifest', '-')
    assert cli.run(conn, args) != 0


def test_complete_rollback_publishes_and_canonically_verifies(rig, monkeypatch):
    def transport(cmd, **kwargs):
        if '--priority-operation apply --writer c' in cmd[-1]:
            return SimpleNamespace(returncode=1, stdout='', stderr='CAS refusal')
        return rig.transport(cmd, **kwargs)
    monkeypatch.setattr(runner.subprocess, 'run', transport)
    code, report = runner.run(None, rig.args)
    assert code == 1 and report['canonical_restore'] == {'verified': 3}
    assert rig.events[-4:] == [('publish', 'a'), ('publish', 'b'), ('publish', 'c'), ('verify-restore', 'a')]


def test_publish_hub_error_keeps_receipts(rig, monkeypatch):
    from hermes_cli.kanban_sync_client import HubError
    def refuse(*args, **kwargs):
        raise HubError(503, 'unavailable')
    monkeypatch.setattr(cli, '_publish', refuse)
    code, report = runner.run(None, rig.args)
    assert code == 1 and set(report['applied']) == {'a', 'b', 'c'}
    assert '503' in report['errors'][0]
    assert not report['restored']
    assert not any(event[0] == 'restore' for event in rig.events)


@pytest.mark.parametrize('exc', [sqlite3.OperationalError('database is locked'), RuntimeError('unexpected')])
def test_local_in_process_apply_exception_still_restores_and_reports(rig, monkeypatch, exc):
    """Review of 806ff390: a non-ValueError from the in-process local writer escaped the
    guarded restore. Any exception must reach restore and still return the JSON receipt."""
    real_apply = executor.apply_attached

    def apply(_conn, attachment, dg, *, writer, undo, allow_legacy_rollback):
        if not undo:
            rig.events.append(('apply', writer))
            raise exc
        return real_apply(_conn, attachment, dg, writer=writer, undo=undo,
                          allow_legacy_rollback=allow_legacy_rollback)
    monkeypatch.setattr(executor, 'apply_attached', apply)
    code, report = runner.run(None, rig.args)
    assert code == 1
    assert report['attempted'] == ['a']
    assert not report['applied']
    assert set(report['restored']) == {'a'}
    assert ('restore', 'a') in rig.events
    assert not any(e[0] == 'apply' and e[1] != 'a' for e in rig.events)
    assert any('attempting guarded restore' in s and str(exc) in s for s in report['errors'])


@pytest.mark.parametrize('exc', [sqlite3.OperationalError('database is locked'), RuntimeError('unexpected')])
def test_local_restore_exception_does_not_skip_remote_restores(rig, monkeypatch, exc):
    real_apply = executor.apply_attached

    def apply(_conn, attachment, dg, *, writer, undo, allow_legacy_rollback):
        if undo:
            rig.events.append(('restore', writer))
            raise exc
        return real_apply(_conn, attachment, dg, writer=writer, undo=undo,
                          allow_legacy_rollback=allow_legacy_rollback)

    def transport(cmd, **kwargs):
        words = shlex.split(cmd[-1])
        action = words[words.index('--priority-operation') + 1]
        if action == 'apply' and words[words.index('--writer') + 1] == 'c':
            return SimpleNamespace(returncode=1, stdout='', stderr='changed priority')
        return rig.transport(cmd, **kwargs)
    monkeypatch.setattr(executor, 'apply_attached', apply)
    monkeypatch.setattr(runner.subprocess, 'run', transport)
    code, report = runner.run(None, rig.args)
    assert code == 1
    assert report['attempted'] == ['a', 'b', 'c']
    assert set(report['applied']) == {'a', 'b'}
    assert set(report['restored']) == {'b', 'c'}
    assert any(s.startswith('a: guarded restore refused') and str(exc) in s for s in report['errors'])
    assert 'canonical_restore' not in report
