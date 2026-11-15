"""Ref-transaction recovery retains the killed-owner and live-holder fences."""
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import _early_recovery, gitlock, update_receipt


@pytest.mark.parametrize("name", ["HEAD.lock", "refs/heads/main.lock", "packed-refs.lock"])
@pytest.mark.parametrize("owned,held", [(True, False), (True, True), (True, None), (False, False)])
def test_ref_recovery_requires_dead_owner(tmp_path, monkeypatch, name, owned, held):
    root = tmp_path / "repo"
    lock = root / ".git" / name
    lock.parent.mkdir(parents=True)
    lock.touch()
    monkeypatch.setattr(gitlock, "_killed_update_owns", lambda candidate: owned)
    monkeypatch.setattr(gitlock, "_windows_git_in_checkout", lambda root: held)
    monkeypatch.setattr(_early_recovery, "_held_open", lambda *args, **kwargs: held)
    result = gitlock.release_dead_fetch_locks(root)
    expected = owned and held is False
    assert result == ([str(lock)] if expected else [])
    assert lock.exists() is not expected


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux holder observation")
def test_real_head_lock_holder_survives_then_dead_owner_is_reclaimed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    lock = root / ".git" / "HEAD.lock"
    lock.parent.mkdir(parents=True)
    monkeypatch.setattr(gitlock, "_killed_update_owns", lambda candidate: True)
    script = "import sys; f=open(sys.argv[1], 'w'); print('ready', flush=True); sys.stdin.read()"
    proc = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert gitlock.release_dead_fetch_locks(root) == []
        assert lock.exists()
    finally:
        proc.communicate(timeout=10)
    assert gitlock.release_dead_fetch_locks(root) == [str(lock)]
    assert not lock.exists()


@pytest.mark.parametrize("name", ["HEAD.lock", "refs/heads/main.lock", "packed-refs.lock"])
def test_ref_lock_failure_has_specific_class(name):
    output = f"fatal: cannot lock ref 'HEAD': Unable to create '/repo/.git/{name}': File exists."
    assert update_receipt.git_output_stop_class(output, ["git", "merge", "--ff-only"]) == "ref_lock_stale"


@pytest.mark.parametrize("output,expected", [
    ("Unable to create '/repo/.git/index.lock': File exists.", "git_index_locked"),
    ("Unable to create '/repo/.git/HEAD.lock': Permission denied", "permission_denied"),
    ("cannot lock ref 'HEAD': is at aaa but expected bbb", "checkout_move_failed"),
])
def test_non_stale_ref_errors_keep_their_class(output, expected):
    assert update_receipt.git_output_stop_class(output, ["git", "merge"]) == expected
