"""Host-local GC extends the existing collector, retaining age and lock guards."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from pm.filesystem import lock_fd
from pm.install_states import collect_orphan_install_states


def _age(state):
    old = time.time() - 7200
    for root, dirs, files in os.walk(state):
        for name in dirs + files:
            os.utime(Path(root) / name, (old, old), follow_symlinks=False)
    os.utime(state, (old, old))


def _state(tmp_path, recorded=True):
    state = tmp_path / "data" / "installs" / "abc123"
    (state / "inputs").mkdir(parents=True)
    root = tmp_path / "code" / "_wt" / "gone"
    if recorded:
        (state / "inputs" / ".project-root").write_text(str(root))
    (state / ".install.lock").touch()
    (state / "environments" / "gen").mkdir(parents=True)
    (state / "environments" / "gen" / "payload").write_bytes(b"x" * 8192)
    _age(state)
    return state, root


@pytest.mark.parametrize("case", ["gone", "live", "locked", "recent", "unknown", "dry-run"])
def test_hourly_collection_contract(tmp_path, caplog, case):
    state, root = _state(tmp_path, recorded=case != "unknown")
    fd = None
    if case == "live":
        root.mkdir(parents=True)
    elif case == "recent":
        state.touch()
    elif case == "locked":
        fd = os.open(state / ".install.lock", os.O_RDWR)
        assert lock_fd(fd, wait=False)
    try:
        with caplog.at_level("INFO"):
            removed = collect_orphan_install_states(
                state.parent, all_missing_roots=True, dry_run=case == "dry-run")
        assert removed == ([state] if case == "gone" else [])
        assert state.exists() == (case != "gone")
        if case == "unknown":
            assert "no recorded root" in caplog.text
        if case == "dry-run":
            assert "would remove" in caplog.text
    finally:
        if fd is not None:
            os.close(fd)


def test_nested_recent_touch_is_kept(tmp_path):
    state, _ = _state(tmp_path)
    (state / "environments" / "gen" / "payload").touch()
    assert collect_orphan_install_states(state.parent, all_missing_roots=True) == []
    assert state.is_dir()


def test_generation_lease_keeps_idle_orphan(tmp_path):
    state, _ = _state(tmp_path)
    leases = state / "environments" / "gen" / ".leases"
    leases.mkdir()
    fd = os.open(leases / "reader", os.O_CREAT | os.O_RDWR, 0o600)
    _age(state)
    assert lock_fd(fd, wait=False)
    try:
        assert collect_orphan_install_states(state.parent, all_missing_roots=True) == []
    finally:
        os.close(fd)
    assert collect_orphan_install_states(state.parent, all_missing_roots=True) == [state]


def test_cli_dry_run_then_sweep_then_empty(tmp_path):
    state, _ = _state(tmp_path)
    command = [sys.executable, "-m", "pm.install_states", "--installs", str(state.parent),
               "--all-missing-roots"]
    dry = subprocess.run([*command, "--dry-run"], capture_output=True, text=True, check=True)
    assert "would remove" in dry.stdout and "removed 0" in dry.stdout
    assert state.exists()
    swept = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "removed 1" in swept.stdout and "GiB" in swept.stdout
    assert not state.exists()
    again = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "removed 0" in again.stdout


def test_install_lock_remains_held_during_deletion(tmp_path, monkeypatch):
    from pm import install_states
    state, _ = _state(tmp_path)
    remove = install_states.shutil.rmtree
    probed = []

    def observed(path, *args, **kwargs):
        if Path(path) == state / "environments":
            fd = os.open(state / ".install.lock", os.O_RDWR)
            try:
                probed.append(not lock_fd(fd, wait=False))
            finally:
                os.close(fd)
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(install_states.shutil, "rmtree", observed)
    assert collect_orphan_install_states(state.parent, all_missing_roots=True) == [state]
    assert probed == [True]


def test_dry_run_preserves_unlocked_lease_and_absent_lock(tmp_path):
    state, _ = _state(tmp_path)
    (state / ".install.lock").unlink()
    leases = state / "environments" / "gen" / ".leases"
    leases.mkdir()
    (leases / "abandoned").touch()
    _age(state)
    before = {str(p.relative_to(state)): p.lstat().st_mtime_ns for p in state.rglob("*")}
    assert collect_orphan_install_states(state.parent, all_missing_roots=True, dry_run=True) == []
    after = {str(p.relative_to(state)): p.lstat().st_mtime_ns for p in state.rglob("*")}
    assert after == before
    assert not (state / ".install.lock").exists()
