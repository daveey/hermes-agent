"""Capsule permission requires installed, unfiltered process provenance."""
import pytest

from gateway import capsule_contract as contract
from hermes_cli import code_attestation, git_fingerprint
from hermes_cli.kanban_capsule_owner import CONTRACT_CAPABILITY
from hermes_state_runtime import RuntimeStoreError


@pytest.fixture
def observation(monkeypatch):
    """Unit boundary only: native ledger evidence lives in attestation subprocess tests."""
    sha = 'a' * 40
    value = {'core_root': str(contract.CORE_ROOT), 'boot_sha': sha,
             'route': 'managed-stub', 'generation': 1, 'unknowns': ()}
    monkeypatch.setattr(git_fingerprint, 'capsule_code_sha', lambda root: sha)
    monkeypatch.setattr(code_attestation, 'integrity_observation', lambda: dict(value))
    monkeypatch.setattr(contract, '_worker_expected_sha', None)
    return value


@pytest.mark.parametrize('defect', ['missing', 'root', 'sha', 'route', 'unknown', 'pending'])
def test_unverified_ledger_withdraws_and_refuses_owner(observation, monkeypatch, defect):
    if defect == 'missing':
        def missing():
            raise RuntimeError('code attestation not installed')
        monkeypatch.setattr(code_attestation, 'integrity_observation', missing)
    else:
        key, value = {'root': ('core_root', '/foreign'), 'sha': ('boot_sha', 'b' * 40),
                      'route': ('route', None), 'unknown': ('unknowns', ('unobserved-code:x',)),
                      'pending': ('unknowns', ('provenance-pending',))}[defect]
        observation[key] = value
    assert CONTRACT_CAPABILITY not in contract.available_capabilities([CONTRACT_CAPABILITY], advertised_sha='a' * 40)
    with pytest.raises(RuntimeStoreError, match='^capsule_owner_code_unverified$'):
        contract.owner_launch_sha(True)


def test_clean_installed_owner_identity_is_usable(observation):
    assert contract.owner_launch_sha(True) == observation['boot_sha']
    assert contract.available_capabilities([CONTRACT_CAPABILITY], advertised_sha='a' * 40) == [CONTRACT_CAPABILITY]


def test_worker_requires_worker_route_even_with_clean_owner_ledger(observation):
    with pytest.raises(RuntimeError, match='^capsule_worker_code_unverified$'):
        contract.install_worker_contract(['--observation-fd', '9', contract.WORKER_FLAG, observation['boot_sha']])


@pytest.mark.parametrize('argv', [
    ['--observation-fd', '9'],
    ['--observation-fd', '9', '--capsule-contract-v3', 'a' * 40],
])
def test_observed_worker_cannot_lose_owner_contract(argv, observation):
    with pytest.raises(RuntimeError, match='^capsule_worker_code_unverified$'):
        contract.install_worker_contract(argv)


def test_ordinary_worker_needs_no_contract(observation):
    assert contract.install_worker_contract([]) == []


def test_inventory_spanning_disk_update_refuses(observation, monkeypatch):
    values = iter([observation['boot_sha'], None])
    monkeypatch.setattr(git_fingerprint, 'capsule_code_sha', lambda root: next(values))
    assert contract._verified_sha() is None


@pytest.mark.parametrize('route', ['managed-stub', 'managed-stub-relaunch', 'console-main-first', None])
def test_worker_route_never_accepts_owner_routes(observation, route):
    observation['route'] = route
    assert contract._verified_sha(worker=True) is None



def test_worker_rechecks_unfiltered_unknowns_before_execution(observation):
    observation['route'] = 'managed-worker-stub'
    contract.install_worker_contract(['--observation-fd', '9', contract.WORKER_FLAG, observation['boot_sha']])
    observation['unknowns'] = ('executed-dirty-then-reverted',)
    with pytest.raises(RuntimeError, match='^capsule_worker_code_unverified$'):
        contract.check_worker_identity()
