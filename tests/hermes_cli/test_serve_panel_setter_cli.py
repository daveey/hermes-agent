"""Real argparse + dispatcher subprocess for hermes producer-mode."""
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli.serve_producer_mode import read_mode


def test_hermes_dispatcher_subprocess_sets_panel_mode(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'HERMES_HOME': str(tmp_path), 'PYTHONPATH': str(root)}
    proc = subprocess.run(
        [sys.executable, '-m', 'hermes_cli.serve_producer_mode',
         '--panel-external', 'true', '--installation', str(tmp_path)],
        check=False, capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)['external'] is True
    off = subprocess.run(
        [sys.executable, '-m', 'hermes_cli.serve_producer_mode',
         '--panel-external', 'false', '--installation', str(tmp_path)],
        check=False, capture_output=True, text=True, env=env, timeout=30)
    assert off.returncode == 0, off.stderr
    assert json.loads(off.stdout)['external'] is False
    assert read_mode(tmp_path)['external'] is False
    # Alias: true/false are the only supported values (no second boolean).
    bad = subprocess.run(
        [sys.executable, '-m', 'hermes_cli.serve_producer_mode',
         '--panel-external', 'off', '--installation', str(tmp_path)],
        check=False, capture_output=True, text=True, env=env, timeout=30)
    assert bad.returncode != 0


def test_named_profile_cli_uses_installation_root_for_default_and_explicit_home(tmp_path):
    root = Path(__file__).resolve().parents[2]
    installation = tmp_path / '.hermes'
    home = installation / 'profiles' / 'named'
    home.mkdir(parents=True)
    env = {**os.environ, 'HERMES_HOME': str(home), 'PYTHONPATH': str(root)}
    for entry in ('hermes_cli.serve_producer_mode', 'hermes_cli.main'):
        for target in ([], ['--installation', str(home)]):
            argv = [sys.executable, '-m', entry]
            if entry == 'hermes_cli.main':
                argv += ['producer-mode']
            proc = subprocess.run(argv + ['--panel-external', 'true', *target], check=False,
                                  capture_output=True, text=True, env=env, timeout=60)
            assert proc.returncode == 0, proc.stderr
            assert json.loads(proc.stdout)['external'] is True
            assert read_mode(installation)['external'] is True
            assert not (home / 'serve-producers').exists()
