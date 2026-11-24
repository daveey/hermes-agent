"""Selected-backend outages retain the real shared notification loop."""
import queue
import threading

import pytest

from hermes_cli import config, config_backend, fact_source_watch
from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch
from tui_gateway import notification_gateway_sources as sources
from tui_gateway import notification_loaded_f as loaded


def test_backend_outage_shared_loop_survives_and_quietly_recovers(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'config.yaml'
    monkeypatch.setattr(config, 'get_config_path', lambda: path)
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    monkeypatch.setattr(sources, 'inventory', lambda: ((tmp_path / 'board.db',), ()))
    config.atomic_config_write(path, {'display': {'gateway_notification_f': True}})
    now = [0]
    scheduler = loaded.Scheduler(clock=lambda: now[0])
    owner = scheduler.gateway_sources = sources.Sources(scheduler.event)
    action = owner.add(lambda: None)
    completed = queue.Queue()
    step = scheduler.step

    def observed_step():
        step()
        completed.put(now[0])

    monkeypatch.setattr(scheduler, 'step', observed_step)
    original_version = config.config_cache_version
    reads = []

    def unavailable(*args, **kwargs):
        reads.append(1)
        raise config_backend.ConfigBackendUnavailable('temporary selected backend outage')

    try:
        scheduler.start(native=False)
        assert completed.get(timeout=5) == 0
        assert owner.policy.enabled and owner.ready
        assert action.due(legacy_due=False)
        old_watch = owner.watch
        monkeypatch.setattr(config, 'config_cache_version', unavailable)
        now[0] = 1
        owner.policy.changed()
        assert completed.get(timeout=5) == 1
        assert scheduler.thread.is_alive()
        assert not owner.policy.enabled and not owner.ready
        assert old_watch.closed and owner.watch is None
        assert owner.retry_at == owner.policy.retry_at == 2
        assert not action.due(legacy_due=False)
        assert reads == [1]
        now[0] = 1.5
        scheduler.event.set()
        assert completed.get(timeout=5) == 1.5
        assert reads == [1]  # unrelated wake cannot accelerate recovery I/O
        monkeypatch.setattr(config, 'config_cache_version', original_version)
        now[0] = 2
        scheduler.event.set()  # deadline wake only; no new source/config hint
        assert completed.get(timeout=5) == 2
        assert scheduler.thread.is_alive()
        assert owner.policy.enabled and owner.ready
        assert owner.retry_at == owner.policy.retry_at == float('inf')
        assert owner.watch is not old_watch
        assert action.due(legacy_due=False)
        assert scheduler.next_reconcile == 30
    finally:
        scheduler.close()
    assert not scheduler.thread.is_alive()


def test_unrelated_system_exit_is_not_policy_recovery(monkeypatch):
    owner = sources.Sources(threading.Event())
    owner.add(lambda: None)

    def stop(*args, **kwargs):
        raise SystemExit('intentional shutdown')

    monkeypatch.setattr(config, 'get_config_path', stop)
    try:
        with pytest.raises(SystemExit, match='intentional shutdown'):
            owner.step(0, reconcile=True)
    finally:
        owner.close()
