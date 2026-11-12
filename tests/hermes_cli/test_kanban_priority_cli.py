"""Operator CLI exercises the real executor on isolated primary fixtures."""
import argparse
import json

import pytest

from hermes_cli import kanban_parser
from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import test_kanban_priority_executor as helpers
from tests.hermes_cli import test_kanban_sync_client as sync_helpers

kanban_home = sync_helpers.kanban_home
conn = sync_helpers.live


def args(*words):
    parser = argparse.ArgumentParser()
    kanban_parser.build_parser(parser.add_subparsers())
    return parser.parse_args(['kanban', 'supervisor-requests', *words])


def test_cli_stage_apply_restore(conn, tmp_path, capsys):
    from hermes_cli import kanban_priority_cli as cli
    ids, _, _ = helpers.setup_plan(conn)
    rows = [dict(r) for r in conn.execute('SELECT id,priority,created_on,status FROM tasks')]
    manifest = migration.plan_global(rows, board_id=sync_helpers.BOARD, exceptions=[ids[2]])
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(manifest, separators=(',', ':')))
    backup = tmp_path / 'backup.json'
    staged = args('--priority-operation', 'stage', '--manifest', str(source), '--backup', str(backup),
                  '--task', ids[0], '--writer', sync_helpers.ME)
    assert cli.run(conn, staged) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert backup.read_bytes() == source.read_bytes()
    common = ['--attachment-id', str(receipt['attachment_id']), '--digest', receipt['manifest_sha256'],
              '--writer', sync_helpers.ME]
    assert cli.run(conn, args('--priority-operation', 'apply', *common)) == 0
    assert helpers.helpers.priorities(conn, ids) == [899, 10000, 900]
    assert cli.run(conn, args('--priority-operation', 'restore', *common)) == 0  # capped: never above 1000
    assert helpers.helpers.priorities(conn, ids) == [1000, 10000, 1000]
    assert cli.run(conn, args('--priority-operation', 'apply', *common)) == 0
    assert cli.run(conn, args('--priority-operation', 'restore', '--allow-legacy-rollback', *common)) == 0
    assert helpers.helpers.priorities(conn, ids) == [10000, 10000, 50000]


def test_cli_worker_refuses_before_opening_manifest(conn, monkeypatch):
    from hermes_cli import kanban_priority_cli as cli
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    assert cli.run(conn, args('--priority-operation', 'stage', '--manifest', '/nonexistent')) != 0
    from hermes_cli import kanban_supervisor_requests as supervisor
    from hermes_cli import kanban_db_connect as kbc
    def forbidden_connect():
        raise AssertionError('worker reached database connect')
    monkeypatch.setattr(kbc, 'connect_closing', forbidden_connect)
    assert supervisor.cli_command(args('--priority-operation', 'prepare')) != 0


@pytest.mark.parametrize('extra, message', [
    (['--drain'], 'cannot combine'),
    (['--status', 'pending'], 'cannot combine'),
    ([], 'attachment-id'),
])
def test_cli_invalid_apply_arguments_do_not_mutate(conn, capsys, extra, message):
    from hermes_cli import kanban_priority_cli as cli
    ids, _, _ = helpers.setup_plan(conn)
    assert cli.run(conn, args('--priority-operation', 'apply', '--writer', sync_helpers.ME, *extra)) != 0
    captured = capsys.readouterr()
    assert message in captured.err + captured.out
    assert helpers.helpers.priorities(conn, ids) == [10000, 10000, 50000]


def test_prepare_cli_reads_enrolled_canonical_board(conn, monkeypatch, tmp_path, capsys):
    from hermes_cli import kanban_priority_cli as cli
    from hermes_cli import kanban_sync_client as sync
    from tests.hermes_cli.test_kanban_priority_prepare import Pages
    ids, _, _ = helpers.setup_plan(conn)
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('https://example.test', 'fixture', sync_helpers.ME))
    monkeypatch.setattr(sync, 'Hub', lambda identity: Pages())
    path = tmp_path / 'canonical.json'
    assert cli.run(conn, args('--priority-operation', 'prepare', '--writer', sync_helpers.ME,
                             '--task', ids[0], '--backup', str(path), '--exception', 'b')) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['attachment_id'] > 0
    manifest = json.loads(path.read_bytes())
    assert manifest['rows'][0]['old'] == 10000
    assert manifest['exceptions'][0]['status'] == 'done'
    verify_args = args('--priority-operation', 'verify-restore', '--writer', sync_helpers.ME,
                       '--manifest', str(path), '--digest', receipt['manifest_sha256'])
    assert cli.run(conn, verify_args) == 0
    assert json.loads(capsys.readouterr().out)['verified'] == 1
    verify_args.digest = 'wrong'
    assert cli.run(conn, verify_args) != 0


@pytest.mark.parametrize('words, message', [
    (['--priority-operation', 'apply'], 'writer'),
    (['--priority-operation', 'stage'], 'task'),
    (['--priority-operation', 'stage', '--task', 'local', '--backup', 'unused'], 'manifest'),
    (['--priority-operation', 'prepare', '--task', 'local', '--backup', 'unused', '--manifest', 'unused'], 'canonical'),
    (['--priority-operation', 'apply', '--attachment-id', '1', '--digest', 'x', '--manifest', 'unused'], 'previously attached'),
    (['--priority-operation', 'verify'], 'manifest'),
    (['--priority-operation', 'verify', '--manifest', 'unused', '--digest', 'x', '--backup', 'unused'], 'only manifest'),
])
def test_cli_validation_precedes_io(conn, capsys, words, message):
    from hermes_cli import kanban_priority_cli as cli
    namespace = args(*words)
    if message != 'writer':
        namespace.writer = sync_helpers.ME
    assert cli.run(conn, namespace) != 0
    output = capsys.readouterr()
    assert message in output.err + output.out


@pytest.mark.parametrize('case', ['identity', 'local_writer', 'shadow', 'board'])
def test_prepare_refuses_wrong_enrollment_before_fetch(conn, monkeypatch, tmp_path, capsys, case):
    from hermes_cli import kanban_priority_cli as cli
    from hermes_cli import kanban_sync_client as sync
    ids, _, _ = helpers.setup_plan(conn)
    gateway = 'foreign' if case == 'identity' else sync_helpers.ME
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('https://example.test', 'fixture', gateway))
    def forbidden_hub(*args):
        pytest.fail('invalid enrollment reached canonical fetch')
    monkeypatch.setattr(sync, 'Hub', forbidden_hub)
    sql = {'local_writer': "UPDATE kanban_sync_state SET gateway_id='foreign'",
           'shadow': "UPDATE kanban_sync_state SET mode='shadow'",
           'board': "UPDATE kanban_sync_state SET board_id=''"}
    if case in sql:
        conn.execute(sql[case])
        conn.commit()
    path = tmp_path / 'refused.json'
    assert cli.run(conn, args('--priority-operation', 'prepare', '--writer', sync_helpers.ME,
                             '--task', ids[0], '--backup', str(path))) != 0
    assert not path.exists()
    output = capsys.readouterr()
    assert ('gateway' in output.out + output.err) or case in ('shadow', 'board')
