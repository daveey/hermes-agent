"""The real identify handler withdraws an unavailable capsule contract."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.runtime_bootstrap import TicketStore
from hermes_cli import git_fingerprint as identity
from tests.hermes_cli import test_capsule_code_identity as helpers

repo = helpers.repo


def test_ready_publication_offers_versioned_contract_to_identify(monkeypatch):
    from gateway.run_runtime import publish_gateway_runtime_ready
    from hermes_cli.kanban_capsule_owner import CAPABILITY, CONTRACT_CAPABILITY
    calls = []
    monkeypatch.setattr('gateway.session_hosted_service.start_ready_hosted_services', lambda runner: calls.append(runner))
    runner = SimpleNamespace(session_runtime_descriptor={}, _running=True, _draining=False,
                             session_api=SimpleNamespace(task=SimpleNamespace(done=lambda: False)))
    publish_gateway_runtime_ready(runner)
    assert runner.session_runtime_descriptor['state'] == 'ready'
    assert {CAPABILITY, CONTRACT_CAPABILITY} <= set(runner.session_runtime_descriptor['capabilities'])
    assert calls == [runner]


@pytest.mark.parametrize('advertised', ['current', 'b' * 40, None, 'invalid'])
def test_identify_dynamically_withdraws_contract(repo, tmp_path, monkeypatch, advertised):
    from gateway.run_bootstrap import _start_gateway_start_control_socket
    from gateway.control_socket import identify_gateway
    from hermes_cli.kanban_capsule_owner import CAPABILITY, CONTRACT_CAPABILITY

    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    actual = identity.capsule_code_sha
    monkeypatch.setattr(identity, 'capsule_code_sha', lambda root: actual(repo))
    # Unit control-socket boundary: the real installed-ledger subprocess is
    # covered separately, never reconstructed in the production permission gate.
    from gateway import capsule_contract as contract
    monkeypatch.setattr('hermes_cli.code_attestation.integrity_observation', lambda **kwargs: {
        'core_root': str(contract.CORE_ROOT), 'boot_sha': actual(repo),
        'route': 'managed-stub', 'unknowns': ()})
    monkeypatch.setattr('gateway.control_socket.build_identify_payload', lambda: {
        'code_sha': actual(repo) if advertised == 'current' else advertised})
    home = repo.parent / (repo.name + '-home')
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME', str(home))
    descriptor = {'instance_id': 'fixture', 'runtime_protocol': 1, 'authority_epoch': 'epoch',
                  'state': 'ready', 'capabilities': [CAPABILITY, CONTRACT_CAPABILITY]}
    runner = SimpleNamespace(session_runtime_descriptor=descriptor, _draining=False,
                             session_ticket_store=TicketStore('fixture', frozenset({str(home)})),
                             config=SimpleNamespace(multiplex_profiles=False))

    async def scenario():
        server = await _start_gateway_start_control_socket(runner)
        assert server is not None
        try:
            before = await asyncio.to_thread(identify_gateway, home)
            (repo / 'module.py').write_text('VALUE = 2\n')
            after = await asyncio.to_thread(identify_gateway, home)
            return before, after
        finally:
            await server.stop()

    before, after = asyncio.run(scenario())
    assert (CONTRACT_CAPABILITY in before['capabilities']) == (advertised == 'current')
    assert CONTRACT_CAPABILITY not in after['capabilities']
    assert CAPABILITY in after['capabilities']
    assert descriptor['capabilities'] == [CAPABILITY, CONTRACT_CAPABILITY]


def test_prelaunch_skew_refuses_without_consuming_rendezvous(repo, monkeypatch):
    from gateway import capsule_contract as contract, session_kanban_capsule as capsule
    from gateway import session_kanban_scope as scope
    from hermes_state_runtime import RuntimeStoreError
    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    monkeypatch.setattr(contract, 'CORE_ROOT', repo)
    (repo / 'module.py').write_text('VALUE = 2\n')
    calls = []
    monkeypatch.setattr(capsule, 'check_capsule_transport', lambda policy: True)
    monkeypatch.setattr(capsule, 'attach_capsule_transport', lambda policy: calls.append('consume'))
    monkeypatch.setattr(scope, 'resolve_run_scope', lambda policy: calls.append('scope'))
    import pytest
    with pytest.raises(RuntimeStoreError, match='^capsule_owner_code_unverified$'):
        scope.placing_spawn(lambda *args, **kwargs: calls.append('spawn'), None)(['worker'])
    assert calls == []


def test_observing_spawn_passes_only_owner_derived_expectation():
    from gateway.session_kanban_rendezvous import observing_spawn
    class ChildEnd:
        def close(self):
            pass
    class Rendezvous:
        def prepare(self, argv, kwargs):
            return ChildEnd(), [*argv, '--observation-fd', '9'], kwargs
    seen = []
    observing_spawn(lambda argv, **kwargs: seen.append(argv), Rendezvous(), owner_sha='a' * 40)(['worker'])
    assert seen == [['worker', '--observation-fd', '9', '--capsule-contract-v2', 'a' * 40]]
