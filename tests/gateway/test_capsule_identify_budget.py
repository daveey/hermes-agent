"""Slow advisory verification must not hide the control socket's v1 answer."""
import asyncio
import threading
import time
from types import SimpleNamespace

from gateway.runtime_bootstrap import TicketStore


def test_slow_identity_withdraws_only_v2_and_never_reuses_late_positive(tmp_path, monkeypatch):
    from gateway import capsule_contract as contract
    from gateway.control_socket import identify_gateway
    from gateway.run_bootstrap import _start_gateway_start_control_socket
    from hermes_cli.kanban_capsule_owner import CAPABILITY, CONTRACT_CAPABILITY

    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME', str(home))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def slow_identity(**kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        finished.set()
        return 'a' * 40
    monkeypatch.setattr(contract, '_verified_sha', slow_identity)
    monkeypatch.setattr('gateway.control_socket.build_identify_payload', lambda: {'code_sha': 'a' * 40})
    descriptor = {'instance_id': 'fixture', 'runtime_protocol': 1, 'authority_epoch': 'epoch',
                  'state': 'ready', 'capabilities': [CAPABILITY, CONTRACT_CAPABILITY]}
    runner = SimpleNamespace(session_runtime_descriptor=descriptor, _draining=False,
                             session_ticket_store=TicketStore('fixture', frozenset({str(home)})),
                             config=SimpleNamespace(multiplex_profiles=False))

    async def scenario():
        server = await _start_gateway_start_control_socket(runner)
        assert server is not None
        try:
            started = time.monotonic()
            answer = await asyncio.to_thread(identify_gateway, home)
            assert answer is not None, 'slow v2 check lost the entire default-timeout identify answer'
            assert time.monotonic() - started < 0.5
            assert entered.is_set()
            assert answer['capabilities'] == [CAPABILITY]
            # A hung verifier must not accumulate threads on subsequent requests.
            for _ in range(3):
                answer = await asyncio.to_thread(identify_gateway, home)
                assert answer is not None
                assert answer['capabilities'] == [CAPABILITY]
            assert len(calls) == 1
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            await server.stop()
    asyncio.run(scenario())
    assert contract._advisory_lock.acquire(timeout=5)
    contract._advisory_lock.release()
    # Even a late success is never a cached positive on a later identify.
    monkeypatch.setattr(contract, '_verified_sha', lambda **kwargs: None)
    assert contract.available_capabilities([CAPABILITY, CONTRACT_CAPABILITY], advertised_sha='a' * 40) == [CAPABILITY]
    monkeypatch.setattr(contract, '_verified_sha', lambda **kwargs: 'a' * 40)
    assert contract.available_capabilities([CAPABILITY, CONTRACT_CAPABILITY], advertised_sha='a' * 40) == [CAPABILITY, CONTRACT_CAPABILITY]
    assert descriptor['capabilities'] == [CAPABILITY, CONTRACT_CAPABILITY]


def test_advisory_preserves_context_and_recovers_from_exception(monkeypatch, caplog):
    import contextvars
    from gateway import capsule_contract as contract
    from hermes_cli.kanban_capsule_owner import CONTRACT_CAPABILITY
    marker = contextvars.ContextVar('capsule-budget-fixture', default='unset')
    token = marker.set('profile-scope')
    seen = []
    def verify(**kwargs):
        seen.append(marker.get())
        raise LookupError('fixture unexpected inventory error')
    try:
        monkeypatch.setattr(contract, '_verified_sha', verify)
        assert contract.available_capabilities([CONTRACT_CAPABILITY], advertised_sha='a' * 40) == []
        assert seen == ['profile-scope']
        assert 'Capsule identify verification failed' in caplog.text
        monkeypatch.setattr(contract, '_verified_sha', lambda **kwargs: 'a' * 40)
        assert contract.available_capabilities([CONTRACT_CAPABILITY], advertised_sha='a' * 40) == [CONTRACT_CAPABILITY]
    finally:
        marker.reset(token)


def test_advisory_thread_start_failure_withdraws_and_releases_slot(monkeypatch):
    from gateway import capsule_contract as contract
    from hermes_cli.kanban_capsule_owner import CONTRACT_CAPABILITY
    class RefusedThread:
        def start(self):
            raise RuntimeError('fixture thread start refused')
    with monkeypatch.context() as scoped:
        scoped.setattr('agent.memory_provider.spawn_context_thread', lambda *a, **k: RefusedThread())
        assert contract.available_capabilities([CONTRACT_CAPABILITY], advertised_sha='a' * 40) == []
    monkeypatch.setattr(contract, '_verified_sha', lambda **kwargs: 'a' * 40)
    assert contract.available_capabilities([CONTRACT_CAPABILITY], advertised_sha='a' * 40) == [CONTRACT_CAPABILITY]


def test_advisory_charges_startup_and_rejects_completed_late_positive(monkeypatch):
    from gateway import capsule_contract as contract
    clock = [10.0]
    monkeypatch.setattr(contract.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(contract, '_verified_sha', lambda **kwargs: 'a' * 40)

    class LateThread:
        def __init__(self, target):
            self.target = target

        def start(self):
            clock[0] += 0.51
            self.target()

    monkeypatch.setattr('agent.memory_provider.spawn_context_thread',
                        lambda target, **kwargs: LateThread(target))
    assert contract._advisory_sha() is None
    assert contract._advisory_lock.acquire(blocking=False)
    contract._advisory_lock.release()
