"""Mode env, master override, and the one runtime setter."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.serve_producer_mode import (
    MASTER_ENV, PANEL_ENV, effective_external, env_initial_external, initialize_mode,
    read_mode, write_mode, build_parser,
)


def test_master_zero_overrides_panel_env(monkeypatch):
    monkeypatch.setenv(PANEL_ENV, 'true')
    monkeypatch.setenv(MASTER_ENV, '0')
    assert env_initial_external() is False
    assert effective_external(Path('/nonexistent')) is False


def test_panel_env_and_file_are_the_same_mode(tmp_path, monkeypatch):
    monkeypatch.setenv(PANEL_ENV, 'true')
    monkeypatch.delenv(MASTER_ENV, raising=False)
    first = initialize_mode(tmp_path, dict(os.environ))
    assert first == {'external': True, 'revision': 1}
    assert effective_external(tmp_path) is True
    second = write_mode(tmp_path, external=False)
    assert second['external'] is False
    assert second['revision'] == 2
    assert effective_external(tmp_path) is False


def test_mode_revision_must_increase(tmp_path):
    write_mode(tmp_path, external=False, revision=1)
    with pytest.raises(ValueError, match='increase'):
        write_mode(tmp_path, external=True, revision=1)


def test_argparse_panel_external_is_required():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(['--panel-external', 'false', '--installation', '/tmp/x'])
    assert args.panel_external == 'false'


def test_cli_subprocess_writes_mode_file(tmp_path):
    env = {**os.environ, 'HERMES_HOME': str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, '-m', 'hermes_cli.serve_producer_mode',
         '--panel-external', 'false', '--installation', str(tmp_path)],
        check=False, capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['external'] is False
    assert payload['revision'] >= 1
    assert read_mode(tmp_path) == payload
