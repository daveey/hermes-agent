"""Exercise the rendered installed launcher against real dispatcher evidence."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from hermes_cli import _launch_template
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_governors as governors
from tests.hermes_cli import test_kanban_governors as helpers

board = helpers.board
clean = helpers.clean
ROOT = Path(__file__).resolve().parents[2]


def _launch(tmp_path, board, args, *, guard=True):
    code = _launch_template.render(str(ROOT), 'hermes_cli.main', 'main')
    # Instrument the real generated launcher without replacing any application modules.
    prelude = '''import sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('pm.environments', 'hermes_cli.venv_sync', 'hermes_cli.plugins',
                        'hermes_cli.main', 'hermes_cli.kanban'):
            raise AssertionError('slow import: ' + fullname)
completed = set()
def profile(frame, event, arg):
    if event == 'return':
        completed.add(frame.f_code.co_name)
sys.setprofile(profile)
def audit(event, args):
    if event == 'subprocess.Popen':
        raise AssertionError('happy path spawned a subprocess')
    if event == 'sqlite3.connect':
        required = {'restore_interrupted_pull', '_settle_interrupted_update',
                    '_record_boot_revision', '_install_code_attestation', '_pin_launcher_home'}
        assert required <= completed, required - completed
        assert 'hermes_cli.code_attestation' in sys.modules
        assert 'hermes_cli.local_work' in sys.modules
sys.addaudithook(audit)
sys.meta_path.insert(0, Guard())
'''
    script = tmp_path / 'hermes'
    script.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-I', '-c', (prelude if guard else '') + code]) + ' "$@"\n')
    script.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_KANBAN_')}
    env['HERMES_KANBAN_HOME'] = str(board)
    env['HERMES_HOME'] = str(board)
    return subprocess.run([str(script), *args], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=30, check=False)


@pytest.mark.parametrize('json_mode', [False, True])
@pytest.mark.parametrize('guard', [False, True])
def test_launcher_fresh_snapshot_before_dependency_activation(board, monkeypatch, tmp_path, capsys, json_mode, guard):
    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='launcher snapshot', assignee='default')
        helpers._tick(conn)
        from hermes_cli.kanban_governors_cli import why
        expected = why(conn, tid)
    args = ['kanban', 'why', tid, '--board', 'default'] + (['--json'] if json_mode else [])
    # guard=False preserves byte-exact managed-stub attestation. The import
    # tripwire prelude changes that route, so keep both as separate controls.
    result = _launch(tmp_path, board, args, guard=guard)
    assert result.returncode == 0, result.stderr
    if not json_mode:
        from hermes_cli.kanban_governors_cli import _print_why
        import re

        capsys.readouterr()
        _print_why(expected)
        expected_text = capsys.readouterr().out
        # Snapshot ages advance between the two reads; all formatting stays identical.
        normalize = lambda text: re.sub(r'\d+(?:\.\d+)?s old', '<age>s old', text)
        assert normalize(result.stdout) == normalize(expected_text)
        return
    actual = json.loads(result.stdout)
    for answer in (actual, expected):
        answer.pop('elapsed_s')
        answer.pop('age_s')
        answer['first_stop'].pop('age_s')
        answer['recorded'].pop('age_s')
        for step in answer['chain']:
            step.pop('age_s', None)
    assert actual == expected


@pytest.mark.parametrize('json_mode', [False, True])
def test_launcher_falls_back_for_missing_stale_and_other_commands(board, monkeypatch, tmp_path, json_mode):
    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='launcher fallback', assignee='default')
        helpers._tick(conn)
        snapshot = governors.snapshot_path(conn)
        original = snapshot.read_text()
    args = ['kanban', 'why', tid, '--board', 'default'] + (['--json'] if json_mode else [])
    # A tripwire at the unchanged slow-path boundary proves fallback, without running commands.
    for other in ([*args, '--live'], ['--version'], ['chat'], ['kanban', 'governors', '--json'],
                  ['kanban', 'why', 'missing'], [*args, '--unknown']):
        result = _launch(tmp_path, board, other)
        assert result.returncode != 0 and 'slow import: pm.environments' in result.stderr, result.stderr
    snapshot.write_text(json.dumps({**json.loads(original), 'tick_at': 0}))
    result = _launch(tmp_path, board, args)
    assert result.returncode != 0 and 'slow import: pm.environments' in result.stderr, result.stderr
    snapshot.unlink()
    result = _launch(tmp_path, board, args)
    assert result.returncode != 0 and 'slow import: pm.environments' in result.stderr, result.stderr
