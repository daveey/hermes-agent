"""Supported producer readback never initializes or mutates a board."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_effective_priority as ep


def test_snapshot_and_cli_use_effective_not_age(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'kanban.db'
    conn = kbc.connect(path)
    tid = kb.create_task(conn, title='requester', priority=37)
    before = list(conn.execute('SELECT * FROM task_events'))
    monkeypatch.setattr(kb, 'kanban_db_path', lambda board=None: path)
    assert ep.snapshot(tid)['priority'] == 37
    ep.main([tid])
    assert json.loads(capsys.readouterr().out)['task_id'] == tid
    assert list(conn.execute('SELECT * FROM task_events')) == before
    conn.close()


def test_missing_board_not_created(tmp_path, monkeypatch):
    path = tmp_path / 'missing.db'
    monkeypatch.setattr(kb, 'kanban_db_path', lambda board=None: path)
    with pytest.raises(SystemExit) as exc:
        ep.main(['absent'])
    assert exc.value.code == 1
    assert not path.exists()


def test_bootstrapped_producer_cli(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    from hermes_cli._launchers import runtime_command
    from tools.environments.local import build_subprocess_env

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    path = kb.kanban_db_path('default')
    conn = kbc.connect(path)
    tid = kb.create_task(conn, title='bootstrapped requester', priority=37)
    conn.close()
    before = path.read_bytes()
    root = Path(__file__).resolve().parents[2]
    command = runtime_command(root, [tid, '--board', 'default'], module='hermes_cli.kanban_effective_priority')
    result = subprocess.run(command, env=build_subprocess_env(), capture_output=True,
                            text=True, encoding='utf-8', timeout=45, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'task_id': tid, 'board': 'default', 'priority': 37}
    assert path.read_bytes() == before


def test_absent_requester_is_not_zero(tmp_path, monkeypatch):
    path = tmp_path / 'kanban.db'
    kbc.connect(path).close()
    monkeypatch.setattr(kb, 'kanban_db_path', lambda board=None: path)
    with pytest.raises(ValueError, match='absent'):
        ep.snapshot('absent')
