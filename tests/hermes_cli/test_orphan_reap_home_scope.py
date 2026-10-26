"""A boot reaper must not cross install or user boundaries."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from hermes_cli import dashboard_procs as procs


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("identity", ["foreign", "unreadable", "missing", "relative", "uid", "uid-unreadable", "ssh-foreign-lock", "ssh-nonce", "profile-override", "same"])
def test_reap_requires_positive_same_home_and_uid(tmp_path, monkeypatch, identity):
    home = tmp_path / "home"
    monkeypatch.setattr(procs, "_hermes_home_dir", lambda: home)
    argv = ["hermes", "serve", "--host", "127.0.0.1", "--port", "0"]
    proc = Mock()
    proc.cmdline.return_value = argv
    proc.uids.return_value = SimpleNamespace(real=os.getuid(), effective=os.geteuid())
    env = {"HERMES_HOME": {"foreign": str(tmp_path / "other"), "missing": "", "relative": "home"}.get(identity, str(home))}
    if identity == "ssh-foreign-lock":
        import json
        from tests.hermes_cli.test_orphan_desktop_serve_reap import _valid_lock_payload
        oid, nonce = "a" * 32, "b" * 16
        owner = tmp_path / "foreign" / "desktop-ssh" / oid
        owner.mkdir(parents=True)
        (owner / "backend.lock.json").write_text(json.dumps(_valid_lock_payload(777, oid, nonce)))
        argv.extend(["--ssh-session-token-file", str(owner / "token")])
    elif identity == "ssh-nonce":
        argv.extend(["--ssh-owner-nonce=" + "b" * 16])
    if identity == "profile-override":
        argv[1:1] = ["--profile", "other"]
    if identity == "uid":
        proc.uids.return_value = SimpleNamespace(real=os.getuid() + 1, effective=os.geteuid())
    elif identity == "uid-unreadable":
        proc.uids.side_effect = psutil.AccessDenied(777)
    monkeypatch.setattr(psutil, "Process", lambda pid: proc)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 1000)
    monkeypatch.setattr(procs, "_pid_environ", lambda pid: None if identity == "unreadable" else env)
    monkeypatch.setattr(procs, "_scan_dashboard_processes", lambda **kw: [(777, " ".join(argv))])
    monkeypatch.setattr(procs, "_process_ppid", lambda pid: 1)
    monkeypatch.setattr(procs, "_posix_descendants", lambda pids: {})
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    kill = Mock()
    monkeypatch.setattr(os, "kill", kill)
    result = procs._reap_orphaned_desktop_local_serves(
        sleep_fn=lambda _: None, lock_owned_pids_fn=lambda: set(),
        process_age_seconds_fn=lambda pid: 600,
    )
    assert result["killed"] == ([777] if identity == "same" else [])
    assert kill.call_count == (2 if identity == "same" else 0)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("change", ["recycled-root", "foreign-child"])
def test_reap_does_not_signal_replaced_root_or_foreign_descendant(monkeypatch, change):
    from gateway import status
    phase = [False]
    monkeypatch.setattr(procs, "_scan_dashboard_processes", lambda **kw: [(777, "hermes serve --host 127.0.0.1 --port 0")])
    monkeypatch.setattr(procs, "_process_ppid", lambda pid: 1)
    monkeypatch.setattr(procs, "_reap_candidate_in_current_home", lambda pid: pid == 777)
    monkeypatch.setattr(procs, "_posix_descendants", lambda pids: {888: (777, 1000)} if change == "foreign-child" else {})
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 2000 if phase[0] and change == "recycled-root" else 1000)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    kill = Mock()
    monkeypatch.setattr(os, "kill", kill)
    procs._reap_orphaned_desktop_local_serves(
        sleep_fn=lambda _: phase.__setitem__(0, True),
        lock_owned_pids_fn=lambda: set(), process_age_seconds_fn=lambda pid: 600,
    )
    assert all(call.args[0] != 888 for call in kill.call_args_list)
    assert kill.call_count == (1 if change == "recycled-root" else 2)


@pytest.mark.platforms("posix")
def test_real_foreign_process_survives_isolated_home_reap(tmp_path, monkeypatch):
    import subprocess
    import sys

    home = tmp_path / "test-home"
    other = tmp_path / "real-home"
    script = tmp_path / "hermes"
    script.write_text("import sys\nsys.stdin.read()\n")
    argv = [sys.executable, str(script), "serve", "--host", "127.0.0.1", "--port", "0"]
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, env={**os.environ, "HERMES_HOME": str(other)})
    try:
        monkeypatch.setattr(procs, "_hermes_home_dir", lambda: home)
        # Restrict discovery to our disposable child; simulate its orphan topology
        # without ever exposing real host processes to the test's signal path.
        monkeypatch.setattr(procs, "_scan_dashboard_processes", lambda **kw: [(child.pid, " ".join(argv))])
        monkeypatch.setattr(procs, "_process_ppid", lambda pid: 1)
        result = procs._reap_orphaned_desktop_local_serves(
            sleep_fn=lambda _: None, lock_owned_pids_fn=lambda: set(),
            process_age_seconds_fn=lambda pid: 600,
        )
        assert result["matched"] == []
        assert child.poll() is None
    finally:
        child.communicate(timeout=10)
