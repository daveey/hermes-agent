"""Cold minting and one-way capability revocation before foreign code executes."""
import os
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import worker_drain_capability as cap
from hermes_cli import worker_drain_fence as fence

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def daemon(monkeypatch):
    fd = os.open('/dev/null', os.O_RDONLY)
    monkeypatch.setattr(cap, '_daemon_fd', None, raising=False)
    monkeypatch.setattr(cap, '_revoked', False, raising=False)
    monkeypatch.setattr(fence, 'load_activation', lambda: {'uid': os.getuid()})
    monkeypatch.setattr(fence, 'current_identity', lambda act: 'identity')
    monkeypatch.setattr(cap, 'create_capability', lambda ident, manifest: os.dup(fd))
    from hermes_cli import worker_drain_baseline as baseline
    monkeypatch.setattr(baseline, 'load_manifest', lambda path: {})
    try:
        yield
    finally:
        cap.revoke('test teardown')
        os.close(fd)


def test_start_retains_descriptor_and_revoke_is_irreversible(daemon):
    assert cap.start_daemon_capability()
    fd = cap._daemon_fd
    os.fstat(fd)
    cap.revoke('unreviewed plugin')
    with pytest.raises(OSError):
        os.fstat(fd)
    assert not cap.start_daemon_capability()
    assert cap._daemon_fd is None


def test_no_activation_never_mints(daemon, monkeypatch):
    monkeypatch.setattr(fence, 'load_activation', lambda: None)
    assert not cap.start_daemon_capability()
    assert cap._daemon_fd is None


def test_mint_failure_does_not_break_gateway(daemon, monkeypatch):
    from hermes_cli.worker_drain_baseline import CaptureRefused
    def refuse(*args):
        raise CaptureRefused('warm startup')
    monkeypatch.setattr(cap, 'create_capability', refuse)
    assert not cap.start_daemon_capability()
    assert cap._daemon_fd is None


def test_cold_boundary_preserves_watchdog_but_rejects_unknown_threads(monkeypatch):
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, '_plugin_manager', None)
    monkeypatch.setattr(plugins, '_plugin_managers_by_home', {})
    main = threading.main_thread()
    monkeypatch.setattr(threading, 'enumerate', lambda: [main, SimpleNamespace(name='gateway-startup-watchdog'),
                                                       SimpleNamespace(name='hermes-exit-metrics')])
    assert cap.startup_is_cold()
    monkeypatch.setattr(threading, 'enumerate', lambda: [main, SimpleNamespace(name='arbitrary-work')])
    assert not cap.startup_is_cold()


def test_entry_calls_capability_before_starting_application(monkeypatch):
    from hermes_cli import gateway
    class BoundaryReached(RuntimeError):
        pass
    def boundary():
        raise BoundaryReached('before application work')
    monkeypatch.setattr(cap, 'start_daemon_capability', boundary)
    for name in ('_guard_official_docker_root_gateway', '_attach_to_host_gateway_or_guard',
                 '_guard_supervised_gateway_conflict', '_guard_existing_gateway_process_conflict',
                 '_apply_startup_watchdog_config'):
        monkeypatch.setattr(gateway, name, lambda *a, **kw: None)
    from hermes_cli.observability import shared_metrics_process
    monkeypatch.setattr(shared_metrics_process, 'begin_process', lambda *a: None)
    def application_started():
        pytest.fail('application started before capability boundary')
    monkeypatch.setattr(gateway, '_stdin_is_tty', application_started)
    with pytest.raises(BoundaryReached):
        gateway.run_gateway()


@pytest.mark.parametrize('method', ['_load_plugin', '_load_directory_module', '_load_entrypoint_module',
                                  '_load_portable_plugin'])
def test_unreviewed_plugin_revokes_before_any_plugin_execution(daemon, monkeypatch, method):
    from hermes_cli.plugins_loader import PluginLoaderMixin
    assert cap.start_daemon_capability()
    original = cap.revoke
    class Revoked(RuntimeError):
        pass
    def boundary(reason):
        original(reason)
        raise Revoked('before plugin code')
    monkeypatch.setattr(cap, 'revoke', boundary)
    args = (object(), object()) if method == '_load_portable_plugin' else (object(),)
    try:
        with pytest.raises(Revoked):
            getattr(PluginLoaderMixin, method)(object(), *args)
        assert cap._daemon_fd is None
    finally:
        monkeypatch.setattr(cap, 'revoke', original)


def test_plugins_loaded_before_mint_make_startup_warm(monkeypatch):
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, '_plugin_managers_by_home', {'home': object()})
    assert not cap.startup_is_cold()
