"""Startup git waits overlap the launch; their answers and refusals do not change."""
import subprocess
import threading
import time

import pytest

from hermes_cli import git_fingerprint as identity
from hermes_cli import version_info


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


def test_begun_capture_returns_before_git_finishes_and_readers_join_it(repo, monkeypatch):
    identity.record_process_boot_fingerprint(repo)
    release = threading.Event()
    finish = identity._capsule_status_finish

    def slow(started):
        release.wait(10)
        return finish(started)

    monkeypatch.setattr(identity, '_capsule_status_finish', slow)
    began = time.monotonic()
    identity.begin_capsule_boot_identity(repo)
    assert time.monotonic() - began < 5
    key = identity._key(repo)
    assert key not in identity._capsule_boot and key in identity._capsule_capturing
    identity.begin_capsule_boot_identity(repo)  # one capture per boot
    identity.record_capsule_boot_identity(repo)  # cannot replace a running capture
    assert key not in identity._capsule_boot
    threading.Timer(0.2, release.set).start()
    monkeypatch.setattr(identity, '_capsule_status_finish', finish)
    assert identity.capsule_code_sha(repo) == git(repo, 'rev-parse', 'HEAD')
    assert key not in identity._capsule_capturing


def test_begun_capture_of_a_dirty_boot_stays_unknown(repo):
    identity.record_process_boot_fingerprint(repo)
    (repo / 'module.py').write_text('VALUE = 2\n')
    identity.begin_capsule_boot_identity(repo)
    assert identity.capsule_code_sha(repo) is None
    git(repo, 'checkout', '--', 'module.py')
    identity.begin_capsule_boot_identity(repo)
    identity.record_capsule_boot_identity(repo)
    assert identity.capsule_code_sha(repo) is None


def test_begun_capture_refuses_a_revision_that_moved_while_git_ran(repo, monkeypatch):
    identity.record_process_boot_fingerprint(repo)
    finish = identity._capsule_status_finish

    def moved(started):
        (repo / 'module.py').write_text('VALUE = 3\n')
        git(repo, 'commit', '-qam', 'moved')
        return finish(started)

    monkeypatch.setattr(identity, '_capsule_status_finish', moved)
    identity.begin_capsule_boot_identity(repo)
    assert identity.capsule_code_sha(repo) is None


def test_capture_lost_with_its_thread_is_unknown_not_retried(repo, monkeypatch):
    identity.record_process_boot_fingerprint(repo)
    key = identity._key(repo)
    lost = threading.Thread(target=lambda: None)
    lost.start()
    lost.join()
    monkeypatch.setitem(identity._capsule_capturing, key, lost)
    assert identity.capsule_code_sha(repo) is None
    identity.record_capsule_boot_identity(repo)
    assert identity.capsule_code_sha(repo) is None


def test_prewarm_resolves_once_and_callers_wait_for_it(monkeypatch):
    calls = []
    release = threading.Event()
    info = version_info.VersionInfo('1.2.3', '1.2.3', 0, 'a' * 40, 'main', 'git')

    def resolve():
        calls.append(threading.current_thread().name)
        release.wait(10)
        return info

    monkeypatch.setattr(version_info, '_cached_version_info', None)
    monkeypatch.setattr(version_info, '_stamp_or_none', resolve)
    version_info.prewarm_version_info()
    limit = time.monotonic() + 5
    while not calls and time.monotonic() < limit:
        time.sleep(0.01)
    assert calls == ['version-info-prewarm']
    threading.Timer(0.2, release.set).start()
    assert version_info.get_version_info() is info
    assert calls == ['version-info-prewarm']
    version_info.prewarm_version_info()  # cached: no second thread
    assert version_info.get_version_info() is info and len(calls) == 1
