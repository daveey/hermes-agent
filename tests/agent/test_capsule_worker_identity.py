"""Owner expectations are verified before any managed execution or observation."""
import pytest

from hermes_cli import git_fingerprint as identity
from tests.hermes_cli import test_capsule_code_identity as helpers

git = helpers.git
repo = helpers.repo


def test_child_refuses_revision_change_before_any_execution(repo, monkeypatch):
    from gateway import capsule_contract as contract
    from agent import managed_worker

    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    sha = git(repo, 'rev-parse', 'HEAD')
    monkeypatch.setattr(contract, 'CORE_ROOT', repo)
    monkeypatch.setattr(contract, '_worker_expected_sha', None, raising=False)
    # Unit execution-boundary fixture, not native attestation evidence.
    monkeypatch.setattr('hermes_cli.code_attestation.integrity_observation', lambda: {
        'core_root': str(repo), 'boot_sha': sha,
        'route': 'managed-worker-stub', 'unknowns': ()})
    assert contract.install_worker_contract(['--observation-fd', '9', '--capsule-contract-v2', sha]) == [
        '--observation-fd', '9']
    (repo / 'module.py').write_text('VALUE = 2\n')
    calls = []
    monkeypatch.setattr(managed_worker, 'bind_worker_policy', lambda frame: calls.append('execution'))
    with pytest.raises(RuntimeError, match='^capsule_worker_code_unverified$'):
        managed_worker.execute({}, None)
    assert calls == []


@pytest.mark.parametrize('sha', ['b' * 40, '', 'invalid'])
def test_child_refuses_wrong_or_unknown_owner_identity(repo, monkeypatch, sha):
    from gateway import capsule_contract as contract
    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    monkeypatch.setattr(contract, 'CORE_ROOT', repo)
    monkeypatch.setattr(contract, '_worker_expected_sha', None, raising=False)
    with pytest.raises(RuntimeError, match='^capsule_worker_code_unverified$'):
        contract.install_worker_contract(['--observation-fd', '9', '--capsule-contract-v2', sha])


def test_lost_expectation_refuses_installed_observation_before_execution(monkeypatch):
    from gateway import capsule_contract as contract
    from agent import managed_worker
    monkeypatch.setattr(contract, '_worker_expected_sha', None)
    monkeypatch.setattr('agent.observation_bind.installed_fd', lambda: 9)
    calls = []
    monkeypatch.setattr(managed_worker, 'bind_worker_policy', lambda frame: calls.append('execution'))
    with pytest.raises(RuntimeError, match='^capsule_worker_code_unverified$'):
        managed_worker.execute({}, None)
    assert calls == []


def test_ordinary_child_does_not_require_capsule_identity(monkeypatch):
    from gateway import capsule_contract as contract
    monkeypatch.setattr(contract, '_worker_expected_sha', None, raising=False)
    monkeypatch.setattr(identity, 'capsule_code_sha', lambda root: pytest.fail('ordinary child identity lookup'))
    assert contract.install_worker_contract([]) == []
    contract.check_worker_identity()
