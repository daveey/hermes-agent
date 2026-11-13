"""Real helper/aux-daemon boundaries; no external provider or coverage assertion."""
import threading

import pytest

from agent import auxiliary_client as aux
from agent.memory_provider import spawn_context_thread
from hermes_cli.local_work import Registry


def test_context_thread_reserves_at_start_and_finishes_physically(tmp_path):
    registry = Registry(homes=[tmp_path])
    entered, release = threading.Event(), threading.Event()

    def target():
        entered.set()
        assert release.wait(5)

    with registry.scope('owner', home=tmp_path, source='test'):
        thread = spawn_context_thread(target, name='local-work-test')
    baseline = registry.snapshot().epoch
    assert registry.snapshot().active == 0
    try:
        thread.start()
        assert entered.wait(5)
        assert registry.snapshot().active == 1
        assert registry.snapshot().epoch > baseline
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert registry.snapshot().active == 0
    assert not registry.snapshot().unknowns


def test_passive_helper_and_unscoped_helper_remain_untracked(tmp_path):
    registry = Registry(homes=[tmp_path])
    with registry.scope('owner', home=tmp_path, source='test'):
        thread = spawn_context_thread(lambda: None, name='passive', passive=True)
    baseline = registry.snapshot().epoch
    thread.start()
    thread.join(5)
    plain = spawn_context_thread(lambda: None, name='unscoped')
    plain.start()
    plain.join(5)
    assert registry.snapshot().epoch == baseline


def test_canceled_protected_owner_keeps_live_provider_counted(tmp_path, monkeypatch):
    registry = Registry(homes=[tmp_path])
    entered, release, owner_done, provider_done = (threading.Event() for _ in range(4))
    cancel = threading.Event()
    errors = []
    threads = []
    monkeypatch.setattr(aux, '_capture_aux_cancel_check', lambda: cancel.is_set)
    monkeypatch.setattr(aux, '_aux_interrupt_protected', lambda: True)

    def provider(kwargs):
        threads.append(threading.current_thread())
        entered.set()
        try:
            assert release.wait(5)
            return 'result'
        finally:
            provider_done.set()

    def owner():
        try:
            with registry.scope('owner', home=tmp_path, source='test'):
                with pytest.raises(aux.AuxiliaryExplicitCancellation):
                    aux._run_protected_sync_provider_call(provider, {})
        except BaseException as exc:
            errors.append(exc)
        finally:
            owner_done.set()

    thread = threading.Thread(target=owner)
    try:
        thread.start()
        assert entered.wait(5)
        cancel.set()
        assert owner_done.wait(5)
        thread.join(5)
        assert not errors
        assert not provider_done.is_set()
        assert registry.snapshot().active == 1
    finally:
        release.set()
        thread.join(5)
        for provider_thread in threads:
            provider_thread.join(5)
    assert provider_done.is_set()
    assert registry.snapshot().active == 0
    assert not registry.snapshot().unknowns
