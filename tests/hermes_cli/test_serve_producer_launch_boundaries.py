"""Execute real launch boundaries, refusing before runtime/plugin imports."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_admission as admission


class BoundaryReached(RuntimeError):
    pass


@pytest.mark.parametrize('module,role,forbidden', [
    ('gateway.run', 'gateway', 'agent.async_utils'),
    ('tui_gateway.entry', 'tui_gateway', 'tui_gateway.server'),
    ('hermes_cli.plugin_host_child', 'plugin_host', 'hermes_cli.plugin_host_wire'),
])
def test_direct_module_boundary_precedes_producer_imports(tmp_path, module, role, forbidden):
    code = '''
import importlib.abc, runpy, sys
from hermes_cli import serve_producer_admission as admission
class Stop(RuntimeError): pass
class Tripwire(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == sys.argv[3]: raise AssertionError('producer imported before admission')
def gated(role):
    assert role == sys.argv[2]
    assert sys.argv[3] not in sys.modules
    raise Stop()
admission.supported_launch = gated
sys.meta_path.insert(0, Tripwire())
try: runpy.run_module(sys.argv[1], run_name='__main__')
except Stop: print('pre-import gate reached')
else: raise AssertionError('launch bypassed gate')
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'), HERMES_RUNTIME_DIR=str(tmp_path / 'pm'))
    result = subprocess.run([sys.executable, '-c', code, module, role, forbidden],
                            cwd=Path(__file__).parents[2], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert 'pre-import gate reached' in result.stdout


@pytest.mark.parametrize('headless,fast', [(False, False), (True, False), (True, True)])
def test_dashboard_and_serve_refuse_before_runtime_preparation(monkeypatch, headless, fast):
    from hermes_cli import main
    for name in ('_dashboard_lifecycle_flags', '_dashboard_sanitize_desktop_env',
                 '_attach_to_host_backend', '_route_named_profile_dashboard'):
        monkeypatch.setattr(main, name, lambda *a: None)
    monkeypatch.setattr(main, '_dashboard_validate_serve_args', lambda *a: None)
    from hermes_cli import resource_limits, serve_update_wait
    monkeypatch.setattr(resource_limits, 'apply_nofile_soft_limit', lambda: None)
    monkeypatch.setattr(serve_update_wait, 'checkout_fingerprint', lambda: None)
    monkeypatch.setattr(serve_update_wait, 'wait_for_update_clear', lambda: None)
    monkeypatch.setattr(serve_update_wait, 'reexec_if_checkout_moved', lambda *a: None)
    observed = []
    def stop(role):
        observed.append(role)
        raise BoundaryReached()
    def forbidden(*a):
        pytest.fail('runtime prepared before launch gate')
    monkeypatch.setattr(admission, 'supported_launch', stop)
    monkeypatch.setattr(main, '_dashboard_prepare_runtime', forbidden)
    from hermes_cli import config
    monkeypatch.setattr(config, 'get_container_exec_info', lambda: None)
    monkeypatch.delenv('HERMES_DISABLE_FAST_SERVE_LAUNCH', raising=False)
    monkeypatch.setattr(sys, 'argv', ['hermes', 'serve', '--no-open'])
    with pytest.raises(BoundaryReached):
        if fast:
            main._try_fast_serve_launch()
        else:
            main.cmd_dashboard(SimpleNamespace(headless_backend=headless))
    assert observed == ['serve' if headless else 'dashboard']


def test_gateway_startup_refuses_before_discovery(monkeypatch):
    from hermes_cli import main, plugins
    for name in ('_apply_safe_mode', '_apply_user_config_bypass', '_guard_noninteractive_user_config'):
        monkeypatch.setattr(main, name, lambda *a: None)
    def stop(role):
        assert role == 'gateway'
        raise BoundaryReached()
    def forbidden():
        pytest.fail('plugin discovery started before launch gate')
    monkeypatch.setattr(admission, 'supported_launch', stop)
    monkeypatch.setattr(plugins, 'start_background_plugin_discovery', forbidden)
    with pytest.raises(BoundaryReached):
        main._prepare_agent_startup(SimpleNamespace(command='gateway', gateway_command='run'))


def test_desktop_spawner_refuses_before_runtime_and_process_spawn(monkeypatch, tmp_path):
    from hermes_cli import gateway_runtime_start
    monkeypatch.setattr(gateway_runtime_start, 'remaining', lambda deadline: 2)
    def stop(role, *, timeout):
        assert role == 'gateway_spawner'
        assert timeout == 2
        raise BoundaryReached()
    def forbidden(*a, **k):
        pytest.fail('spawn attempted before launch gate')
    monkeypatch.setattr(admission, 'supported_launch', stop)
    monkeypatch.setattr(gateway_runtime_start.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(gateway_runtime_start, 'start_gateway_job', forbidden)
    with pytest.raises(BoundaryReached):
        gateway_runtime_start.spawn_unmanaged_gateway(tmp_path, deadline=0)
