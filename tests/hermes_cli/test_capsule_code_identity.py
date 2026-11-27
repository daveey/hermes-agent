"""Strict capsule identity uses real Git state, not restart-ignore heuristics."""
import subprocess

import pytest

from hermes_cli import git_fingerprint as identity


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    (tmp_path / 'module.py').write_text('VALUE = 1\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'initial')
    return tmp_path


@pytest.mark.parametrize('route', [None, 'unsupported-cli', 'managed-stub', 'managed-stub-relaunch',
                                  'console-main-first', 'managed-worker-stub'])
def test_bootstrap_scans_only_installed_supported_routes(repo, monkeypatch, route):
    import hermes_bootstrap
    from hermes_cli import code_attestation, local_work
    monkeypatch.setattr(hermes_bootstrap, '_root', repo)
    calls = []
    capture = identity.begin_capsule_boot_identity
    def record(root):
        calls.append(root)
        capture(root)
    monkeypatch.setattr(identity, 'begin_capsule_boot_identity', record)
    monkeypatch.setattr(code_attestation, 'install', lambda *args: None)
    monkeypatch.setattr(code_attestation, 'route_report', lambda: {'route': route})
    monkeypatch.setattr(local_work, 'install', lambda: None)
    hermes_bootstrap._record_boot_revision()
    assert calls == []  # Bare/unsupported launches must not pay a strict scan.
    hermes_bootstrap._install_code_attestation()
    supported = route not in (None, 'unsupported-cli')
    assert calls == ([repo] if supported else [])
    if supported:
        assert identity.capsule_code_sha(repo) == git(repo, 'rev-parse', 'HEAD')


def test_clean_boot_allows_only_current_revision(repo):
    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    assert identity.capsule_code_sha(repo) == git(repo, 'rev-parse', 'HEAD')
    (repo / 'module.py').write_text('VALUE = 2\n')
    assert identity.capsule_code_sha(repo) is None
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'second-revision')
    assert identity.capsule_code_sha(repo) is None


def test_dirty_boot_is_sticky_after_cleanup(repo):
    identity.record_process_boot_fingerprint(repo)
    (repo / 'module.py').write_text('VALUE = 2\n')
    identity.record_capsule_boot_identity(repo)
    git(repo, 'checkout', '--', 'module.py')
    identity.record_capsule_boot_identity(repo)
    assert identity.capsule_code_sha(repo) is None


def test_unknown_boot_cannot_be_adopted_late(repo):
    identity.record_capsule_boot_identity(repo)
    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    assert identity.capsule_code_sha(repo) is None


def test_restart_free_change_still_withdraws_contract(repo):
    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    (repo / 'README.md').write_text('documentation\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'docs')
    assert identity.capsule_code_sha(repo) is None


@pytest.mark.parametrize('marker', ['index.lock', 'MERGE_HEAD', 'rebase-merge'])
def test_in_progress_update_withdraws_identity(repo, marker):
    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    (repo / '.git' / marker).touch()
    assert identity.capsule_code_sha(repo) is None


def test_untracked_import_cannot_shadow_clean_boot(repo):
    identity.record_process_boot_fingerprint(repo)
    identity.record_capsule_boot_identity(repo)
    (repo / 'shadow.py').write_text('VALUE = 3\n')
    assert identity.capsule_code_sha(repo) is None
