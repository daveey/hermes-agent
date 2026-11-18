"""Deterministic retirement at the spawn boundary, not timing sleeps."""
import json
import os
import socket
from types import SimpleNamespace

import pytest

from tools.origin_cli_bridge import ENV_KEY, Launch


def test_spawn_lease_owns_duplicate_across_retirement():
    launch = Launch(None)
    try:
        with launch.spawn_endpoint() as (fd, locator):
            assert fd != launch.child_fd
            identity = os.fstat(fd)
            assert json.loads(locator)["fd"] == fd
            launch.close()
            # Retiring the offer must not close a descriptor already handed to
            # a delayed Popen, even if unrelated descriptors are allocated now.
            with socket.socket() as unrelated:
                assert unrelated.fileno() != fd
                assert os.fstat(fd) == identity
        with pytest.raises(OSError):
            os.fstat(fd)
        with pytest.raises(RuntimeError, match="retired"):
            with launch.spawn_endpoint():
                pytest.fail("retired launch acquired a spawn endpoint")
    finally:
        launch.close()


def test_duplicate_and_retirement_share_lock(monkeypatch):
    from threading import Event, Lock, Thread, current_thread
    launch = Launch(None)
    duplicating, release, retire_waiting, retired = Event(), Event(), Event(), Event()
    lock = Lock()
    class ObservedLock:
        def __enter__(self):
            if current_thread().name == "retire-probe":
                retire_waiting.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()
    launch._spawn_lock = ObservedLock()
    original = socket.socket.dup
    def slow_dup(sock):
        if sock is launch._child:
            duplicating.set()
            assert release.wait(5)
        return original(sock)
    monkeypatch.setattr(socket.socket, "dup", slow_dup)
    errors = []
    def spawn():
        try:
            with launch.spawn_endpoint():
                pass
        except BaseException as exc:
            errors.append(exc)
    def retire():
        launch.close()
        retired.set()
    spawner = Thread(target=spawn)
    closer = Thread(target=retire, name="retire-probe")
    try:
        spawner.start()
        assert duplicating.wait(5)
        closer.start()
        assert retire_waiting.wait(5)
        assert not retired.is_set()
    finally:
        release.set()
        spawner.join(5)
        if closer.ident is not None:
            closer.join(5)
        launch.close()
    assert not errors
    assert not spawner.is_alive() and not closer.is_alive()


def test_delayed_local_spawn_refuses_after_retire(monkeypatch, tmp_path):
    from tools.environments import local
    env = object.__new__(local.LocalEnvironment)
    env.env, env.cwd = {}, str(tmp_path)
    launch = Launch(None)
    monkeypatch.setattr(env, "_recover_cwd", launch.close)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *a, **kw: pytest.fail("spawned retired offer"))
    try:
        with pytest.raises(RuntimeError, match="retired"):
            env._run_bash("true", origin_cli_launch=launch)
    finally:
        launch.close()


def test_real_popen_after_retirement_keeps_exact_socket(monkeypatch, tmp_path):
    import shlex
    import sys
    from tools.environments import local
    env = object.__new__(local.LocalEnvironment)
    env.env, env.cwd = {}, str(tmp_path)
    launch = Launch(None)
    original = local.subprocess.Popen
    captured = []
    def delayed(*args, **kwargs):
        fd, = kwargs["pass_fds"]
        assert json.loads(kwargs["env"][ENV_KEY])["fd"] == fd
        inode = os.fstat(fd).st_ino
        launch.close()  # between endpoint acquisition and actual Popen
        with socket.socket() as unrelated:
            assert unrelated.fileno() != fd
            proc = original(*args, **kwargs)
        captured.append((fd, inode))
        return proc
    monkeypatch.setattr(local.subprocess, "Popen", delayed)
    code = "import os,json; v=json.loads(os.environ['HERMES_ORIGIN_CLI_BRIDGE']); print(os.fstat(v['fd']).st_ino)"
    try:
        proc = env._run_bash(shlex.join([sys.executable, "-c", code]), origin_cli_launch=launch)
        output, _ = proc.communicate(timeout=5)
        assert proc.returncode == 0
        assert int(output.strip()) == captured[0][1]
        with pytest.raises(OSError):
            os.fstat(captured[0][0])
    finally:
        launch.close()


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_cancellation_retains_admission_report(monkeypatch, error):
    from tools import origin_cli_terminal as boundary
    closed = []
    report = [{"outcome": "outcome_unknown", "task_id": "fixture"}]
    launch = SimpleNamespace(retire=lambda: report, close=lambda: closed.append(True))
    monkeypatch.setattr(boundary, "offer", lambda *a, **kw: launch)
    def fail(*a, **kw):
        raise error("cancel")
    with pytest.raises(error) as caught:
        boundary.execute(SimpleNamespace(execute=fail), "true", env_type="local", task_id=None)
    assert closed == [True]
    assert "outcome_unknown" in " ".join(caught.value.__notes__)
    assert "do not retry" in " ".join(caught.value.__notes__)


@pytest.mark.parametrize("scrub", [False, True])
def test_common_subprocess_factory_drops_stale_locator(scrub):
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env({ENV_KEY: "stale", "PATH": "/usr/bin"},
                               scrub_secrets=scrub, extra={ENV_KEY: "also stale"})
    assert ENV_KEY not in env
