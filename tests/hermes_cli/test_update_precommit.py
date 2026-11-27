"""Deadline protocol with a real isolated updater, Git checkout and custody gate."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from hermes_cli import update_precommit as precommit
from tests.hermes_cli import test_update_up_to as helpers

fork = helpers.fork
pytestmark = pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="POSIX deadline custody protocol")


@pytest.fixture(autouse=True)
def reset_child(monkeypatch):
    for name, value in (("_child_channel", None), ("_child_deadline", None),
                        ("_committed", False), ("_wrote", False)):
        monkeypatch.setattr(precommit, name, value)


@pytest.mark.parametrize("value", ["null", "[]", "{}", '"1"',
    '{"version":true,"clock":"CLOCK_MONOTONIC","deadline":1}',
    '{"version":2,"clock":"CLOCK_MONOTONIC","deadline":1}',
    '{"version":1,"clock":"wall","deadline":1}',
    '{"version":1,"clock":"CLOCK_MONOTONIC","deadline":NaN}',
    '{"version":1,"clock":"CLOCK_MONOTONIC","deadline":true}',
    '{"version":1,"clock":"CLOCK_MONOTONIC","deadline":0}'])
def test_invalid_carrier_refuses_before_spawn(value, monkeypatch, tmp_path):
    monkeypatch.setenv(precommit.DEADLINE_ENV, value)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("invalid carrier spawned"))
    with pytest.raises(SystemExit) as exc:
        precommit.run_if_requested(argparse.Namespace(), tmp_path)
    assert exc.value.code == precommit.REFUSED_EXIT


def test_expired_at_entry_never_spawns(monkeypatch, tmp_path, capsys):
    value = {"version": 1, "clock": "CLOCK_MONOTONIC", "deadline": precommit.clock() - 1}
    monkeypatch.setenv(precommit.DEADLINE_ENV, json.dumps(value))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("expired carrier spawned"))
    with pytest.raises(SystemExit) as exc:
        precommit.run_if_requested(argparse.Namespace(), tmp_path)
    assert exc.value.code == precommit.REFUSED_EXIT
    assert precommit.REFUSAL in capsys.readouterr().out


def test_ordinary_invocation_does_not_spawn(monkeypatch, tmp_path):
    monkeypatch.delenv(precommit.DEADLINE_ENV, raising=False)
    assert precommit.run_if_requested(argparse.Namespace(), tmp_path) is False


def test_late_expiry_blocks_first_checkout_write(fork, monkeypatch):
    from hermes_cli import update_cmd, update_cmd_posix_pause as pause
    monkeypatch.setattr(precommit, "_child_deadline", precommit.clock() + 60)
    # Already handed off custody, then validation/pause consumes the budget.
    monkeypatch.setattr(precommit, "_committed", True)
    def expire():
        monkeypatch.setattr(precommit, "_child_deadline", precommit.clock() - 1)
    monkeypatch.setattr(pause, "pause_at_commit_point", expire)
    before = helpers.head(fork)
    with pytest.raises(SystemExit) as exc:
        helpers.run(fork, "--up-to", fork.c)
    assert exc.value.code == precommit.REFUSED_EXIT
    assert helpers.head(fork) == before
    assert not (fork.root / "c.txt").exists()


def test_expiry_after_first_write_does_not_abandon_install(fork, monkeypatch):
    from hermes_cli import update_cmd
    monkeypatch.setattr(precommit, "_child_deadline", precommit.clock() + 60)
    real_pull = update_cmd._pull_updates
    def pull(*args, **kwargs):
        result = real_pull(*args, **kwargs)
        assert precommit._wrote
        monkeypatch.setattr(precommit, "_child_deadline", precommit.clock() - 1)
        return result
    monkeypatch.setattr(update_cmd, "_pull_updates", pull)
    helpers.run(fork, "--up-to", fork.c)
    assert helpers.head(fork) == (fork.c, "main")
    assert fork.completed[-1]["expected_sha"] == fork.c


def test_deadline_child_cannot_redirect_to_unsupervised_updater(monkeypatch, tmp_path, capsys):
    from hermes_cli import update_owning_install as owning
    monkeypatch.setattr(precommit, "_child_deadline", precommit.clock() + 60)
    monkeypatch.setattr(owning, "owning_install_root", lambda _: tmp_path / "owner")
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: pytest.fail("unsupervised updater spawned"))
    with pytest.raises(SystemExit) as exc:
        owning.retarget_to_owning_install(tmp_path)
    assert exc.value.code == precommit.REFUSED_EXIT
    assert precommit.REFUSAL in capsys.readouterr().out


def _command(fork, mode):
    root = Path(__file__).resolve().parents[2]
    script = str(Path(__file__).resolve())
    code = f"import sys,runpy; sys.path.insert(0,sys.argv[1]); runpy.run_path({script!r},run_name='__main__')"
    return [sys.executable, "-c", code, str(root), str(fork.root), fork.c, mode]


def test_real_preparation_fetch_is_cancelled_with_checkout_unchanged(fork, capfd):
    start = precommit.clock()
    # Child startup has a real budget; the fixture publishes entry before blocking.
    result = precommit.supervise(_command(fork, "fetch"), start + 10, dict(os.environ))
    elapsed = precommit.clock() - start
    assert result == precommit.REFUSED_EXIT
    assert (fork.root.parent / "fetch-entered").exists(), capfd.readouterr()
    assert elapsed < 10  # cancellation and owned-child reaping fit INSIDE original deadline
    assert helpers.head(fork) == (fork.a, "main")
    assert helpers.git(fork.root, "status", "--porcelain") == ""
    assert not (fork.root / ".git" / "hermes-update-pull").exists()
    assert precommit.REFUSAL in capfd.readouterr().out
    from hermes_cli.update_lock import UpdateLock
    lock = UpdateLock(install_root=fork.root)
    assert lock.acquire(), lock.holder
    lock.release()


def test_real_child_live_apply_success_outlives_deadline_in_custody(fork):
    start = precommit.clock()
    result = precommit.supervise(_command(fork, "apply"), start + 8, dict(os.environ))
    assert result == 0
    assert helpers.head(fork) == (fork.c, "main")
    assert (fork.root.parent / "completed").read_text() == fork.c


@pytest.mark.live_system_guard_bypass  # disposable Git fixture, marker-checked driver; no live updater effects

def test_production_command_entry_composes_deadline_and_real_child(fork, tmp_path):
    source = Path(__file__).resolve().parents[2]
    (fork.root / ".precommit-fixture").touch()
    # The marker is fixture infrastructure, not a local edit to stash.
    helpers.git(fork.root, "add", ".precommit-fixture")
    helpers.git(fork.root, "commit", "-qm", "fixture marker")
    before = helpers.head(fork)
    deadline = precommit.clock() + 10
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"),
               HERMES_UPDATE_PRECOMMIT_DEADLINE_V1=json.dumps(
                   {"version": 1, "clock": "CLOCK_MONOTONIC", "deadline": deadline}))
    result = subprocess.run([sys.executable, str(source / "tests/hermes_cli/precommit_driver.py"),
        str(source), str(fork.root), "fetch", "update", "--yes", "--up-to", fork.c],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
    assert result.returncode == precommit.REFUSED_EXIT, result.stdout + result.stderr
    assert (fork.root.parent / "fetch-entered").exists(), result.stdout + result.stderr
    assert precommit.clock() < deadline
    assert helpers.head(fork) == before
    assert precommit.REFUSAL in result.stdout


def test_supervisor_does_not_grant_late_commit(tmp_path):
    code = ("import socket,sys,threading; s=socket.socket(fileno=int(sys.argv[1])); "
            "threading.Event().wait(10); s.sendall(b'C'); "
            "open(sys.argv[0], 'w').write('bad')")
    start = precommit.clock()
    assert precommit.supervise([sys.executable, "-c", code], start + 0.2, dict(os.environ)) == precommit.REFUSED_EXIT
    assert precommit.clock() - start < 2


def test_signal_during_spawn_keeps_child_cleanup_handle(monkeypatch):
    real_spawn, children = subprocess.Popen, []
    def spawn(*args, **kwargs):
        proc = real_spawn(*args, **kwargs)
        children.append(proc)
        signal.raise_signal(signal.SIGTERM)
        return proc
    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = precommit.supervise([sys.executable, "-c", "import threading; threading.Event().wait(60)"],
                                precommit.clock() + 30, dict(os.environ))
    assert result == precommit.REFUSED_EXIT
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize("message", [b"", b"X"])
def test_eof_or_invalid_request_never_commits(message, tmp_path):
    marker = tmp_path / "completed"
    code = ("import socket,sys,threading; s=socket.socket(fileno=int(sys.argv[1])); "
            f"s.sendall({message!r}); s.close(); threading.Event().wait(3); "
            f"open({str(marker)!r}, 'w').write('bad')")
    assert precommit.supervise([sys.executable, "-c", code], precommit.clock() + 5.5,
                               dict(os.environ)) == precommit.REFUSED_EXIT
    assert not marker.exists()


def test_signal_after_commit_ack_keeps_custody_until_child_exit(tmp_path):
    marker = tmp_path / "completed"
    code = ("import os,signal,socket,sys,threading; s=socket.socket(fileno=int(sys.argv[1])); "
            "s.sendall(b'C'); assert s.recv(1)==b'Y'; os.kill(os.getppid(),signal.SIGTERM); "
            "threading.Event().wait(1); "
            f"open({str(marker)!r}, 'w').write('settled')")
    deadline = precommit.clock() + 5.5
    assert precommit.supervise([sys.executable, "-c", code], deadline, dict(os.environ)) == 0
    assert marker.read_text() == "settled"


def test_pause_adoption_waits_until_after_inventory_and_commit_ack(monkeypatch):
    from hermes_cli import update_cmd_posix_pause as pause, update_pause_record as record
    events = []
    monkeypatch.setattr(precommit, "_child_channel", object())
    monkeypatch.setattr(precommit, "_child_deadline", precommit.clock() + 60)
    monkeypatch.setattr(record, "adopt_orphans", lambda: events.append("adopt") or (None, []))
    monkeypatch.setattr(pause, "_discover_systemd", lambda: events.append("inventory") or [])
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_discover_bare", lambda *_: ([], []))
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    monkeypatch.setattr(pause, "_deferral_streak", lambda *_: None)
    monkeypatch.setattr(precommit, "commit_gate", lambda: events.append("ack"))
    token = pause.arm_pause(no_gateway_restart=False)
    assert events == [] and token["precommit_adoption_pending"]
    assert pause.pause_at_commit_point() is None
    assert events == ["inventory", "ack", "adopt"]
    assert "precommit_adoption_pending" not in token


def test_expired_inventory_never_adopts_or_stops(monkeypatch):
    from hermes_cli import update_cmd_posix_pause as pause, update_pause_record as record
    monkeypatch.setattr(precommit, "_child_channel", object())
    monkeypatch.setattr(precommit, "_child_deadline", precommit.clock() + 60)
    monkeypatch.setattr(record, "adopt_orphans", lambda: pytest.fail("cancellable phase adopted restart debt"))
    def inventory():
        precommit._child_deadline = precommit.clock() - 1
        return []
    monkeypatch.setattr(pause, "_discover_systemd", inventory)
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_discover_bare", lambda *_: ([], []))
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    monkeypatch.setattr(pause, "_stop_gateways", lambda *_a, **_k: pytest.fail("expired inventory stopped gateway"))
    pause.arm_pause(no_gateway_restart=False)
    with pytest.raises(SystemExit) as exc:
        pause.pause_at_commit_point()
    assert exc.value.code == precommit.REFUSED_EXIT


def _child():
    """External effects replaced, real updater/fetch/pin/move/custody remain."""
    source, checkout, target, mode, fd, deadline = sys.argv[1:]
    sys.path.insert(0, source)
    from hermes_cli import main, update_cmd, update_cmd_posix_pause as pause
    from hermes_cli.subcommands.update import build_update_parser
    from hermes_cli.update_lock import UpdateLock
    from unittest.mock import patch
    checkout = Path(checkout)
    precommit._child_channel = socket.socket(fileno=int(fd))
    precommit._child_channel.set_inheritable(False)
    precommit._child_deadline = float(deadline)
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
    args = parser.parse_args(["update", "--yes", "--up-to", target, "--no-backup"])
    options = update_cmd._UpdateOptions(pre_update_version=None, gw_input_fn=None,
        assume_yes=True, keep_stash=False, switch_branch=False, discard_local_changes=False)
    def complete(request):
        if mode == "apply":
            # Prove no deadline kill once ACK transferred installation custody.
            remaining = precommit._child_deadline - precommit.clock()
            import threading
            threading.Event().wait(max(0, remaining) + 0.2)  # health: allow HX013 -- expiry during install IS the behavior
        (checkout.parent / "completed").write_text(request["expected_sha"])
    real_git_run = update_cmd._git_run
    def git_run(git_cmd, args, **kwargs):
        if mode == "fetch" and args[0] == "fetch":
            (checkout.parent / "fetch-entered").touch()
            # Same real custody runner and timeout used for the actual fetch,
            # with a controlled remote that cannot finish within this budget.
            from hermes_cli.update_custody import run_git
            return run_git([sys.executable, "-c", "import threading; threading.Event().wait(60)"],
                           [], cwd=checkout, capture_output=True, text=True, encoding="utf-8", timeout=300)
        return real_git_run(git_cmd, args, **kwargs)
    from contextlib import ExitStack
    with ExitStack() as stack:
        patches = [(main, "PROJECT_ROOT", checkout), (main, "_run_pre_update_backup", lambda *_: None),
            (main, "_pause_windows_gateways_for_update", lambda: None),
            (main, "_resume_windows_gateways_after_update", lambda token: None),
            (pause, "_discover_systemd", list), (pause, "_discover_launchd", list),
            (pause, "_discover_bare", lambda pids: ([], [])),
            (update_cmd, "_resolve_update_options", lambda *_: options),
            (update_cmd, "_begin_update_receipt_and_plan", lambda *_: None),
            (update_cmd, "_complete_source_update", complete),
            (update_cmd, "_git_run", git_run)]
        for module, name, value in patches:
            stack.enter_context(patch.object(module, name, value))
        lock = UpdateLock(install_root=checkout)
        assert lock.acquire(), lock.holder
        try:
            update_cmd._cmd_update_impl(args, False)
        finally:
            lock.release()


if __name__ == "__main__":
    _child()
