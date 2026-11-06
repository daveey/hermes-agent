"""An explicitly empty toolset pick is not an absent profile override."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from gateway.session_policy import build_policy
from hermes_cli import main, main_tui_launch
from hermes_cli.gateway_chat import _requested_policy


@pytest.mark.parametrize('explicit', [False, True])
def test_current_z_frozen_policy(tmp_path, explicit):
    args = main._light_chat_parser().parse_args(['-z', 'list tools'] + (['-t', ''] if explicit else []))
    params = {'source': 'cli', 'cwd': str(tmp_path), 'model': 'test', **_requested_policy(args)}
    policy = build_policy(params, {'platform_toolsets': {'cli': ['web']}}, profile_terminal=False)
    assert policy.toolsets == (() if explicit else ('web',))


@pytest.mark.parametrize('explicit', [False, True])
def test_tui_launch_to_frozen_policy(monkeypatch, tmp_path, explicit):
    monkeypatch.setenv('HERMES_PYTHON', sys.executable)
    monkeypatch.delenv('HERMES_TUI_TOOLSETS', raising=False)
    monkeypatch.setattr(main_tui_launch, '_make_tui_argv', lambda *a: (['node', 'unused'], Path('.')))
    captured = {}

    def capture(argv, cwd=None, env=None):
        assert env is not None
        captured.update(env)
        return 1

    monkeypatch.setattr(main.subprocess, 'call', capture)
    with pytest.raises(SystemExit):
        main._launch_tui(toolsets='' if explicit else None)
    assert ('HERMES_TUI_TOOLSETS' in captured) is explicit
    # canonicalGateway.test.ts exercises the env-to-wire translation in the JS suite.
    # Python CI deliberately does not install npm dependencies.
    params = {'source': 'tui', 'cwd': str(tmp_path), 'model': 'test'}
    if explicit:
        assert captured['HERMES_TUI_TOOLSETS'] == ''
        params['toolsets'] = []
    policy = build_policy(params, {'platform_toolsets': {'cli': ['web']}}, profile_terminal=False)
    # An unpinned TUI also receives its built-in project surface toolset.
    assert policy.toolsets == (() if explicit else ('project', 'web'))


@pytest.mark.parametrize(('selection', 'label'), [(None, 'all'), ([], 'none'), (['web'], 'web')])
def test_config_toolset_label(capsys, selection, label):
    from datetime import datetime
    from cli import HermesCLI
    stand_in = SimpleNamespace(api_key=None, agent=None, model='test', base_url='https://example.invalid',
                               enabled_toolsets=selection, max_turns=1, verbose=False, session_start=datetime.now())
    HermesCLI.show_config(stand_in)
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if 'Toolsets' in line)
    assert label in line
