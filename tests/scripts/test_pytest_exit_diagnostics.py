"""Real late-exit diagnostic transport; synthetic hangs are not the historical cause."""
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import run_tests_launch as launch
from tools.environments.local import build_subprocess_env

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "scripts.pytest_exit_diagnostics"


@pytest.mark.parametrize("stage", ["atexit", "finalize"])
def test_late_exit_stacks_survive_capture_and_plugin_cleanup(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(launch, "_fork_server_for_run", lambda root: None)
    test = tmp_path / "test_pass.py"
    test.write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    # Cancel the shared watchdog in unconfigure, as another plugin is allowed to do.
    # The diagnostic wrapper must rearm after all inner teardown hooks return.
    (tmp_path / "conftest.py").write_text('''
import atexit
import faulthandler
import threading

def blocked_shutdown(event=threading.Event()):
    event.wait()

class Finalizer:
    def __del__(self, block=blocked_shutdown):
        block()

def pytest_sessionfinish(session):
    STAGE

def pytest_unconfigure(config):
    faulthandler.cancel_dump_traceback_later()
'''.replace("STAGE", "atexit.register(blocked_shutdown)" if stage == "atexit" else
            "global retained; retained = Finalizer()"), encoding="utf-8")
    env = build_subprocess_env()
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["TMPDIR"] = str(tmp_path)
    proc, _ = launch.start_pytest(
        test, ["-p", PLUGIN, "-o", "exit_diagnostics_delay=0.1", "-q"],
        ROOT, env, str(tmp_path),
    )
    captured = []
    stack_seen = threading.Event()

    def read_output():
        for line in proc.stdout:
            captured.append(line)
            if "in blocked_shutdown" in line:
                stack_seen.set()

    reader = threading.Thread(target=read_output)
    reader.start()
    try:
        assert stack_seen.wait(30), "".join(captured)
        assert proc.poll() is None  # the watchdog observed, rather than terminated, the hang
    finally:
        proc.kill()
        proc.wait(timeout=10)
        reader.join(timeout=10)
        proc.stdout.close()
    assert not reader.is_alive()
    output = "".join(captured)
    assert "1 passed" in output, output
    assert "unconfigure-return" in output, output
    assert "atexit-enter" in output, output
    assert "Timeout (" in output and "in blocked_shutdown" in output, output


@pytest.mark.parametrize("mode", ["sessionfinish", "captured-stderr", "repeat"])
def test_sessionfinish_watchdog_survives_blocked_hook(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(launch, "_fork_server_for_run", lambda root: None)
    test = tmp_path / "test_pass.py"
    test.write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    # A wrapper's post-yield arm runs after the diagnostic sessionfinish hook,
    # but before unconfigure can rearm it. Capture is deliberately left active
    # in one case: neither markers nor watchdog output may depend on fd 2.
    (tmp_path / "conftest.py").write_text('''
import os
import threading
import pytest
from scripts import pytest_exit_diagnostics as diagnostics

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    yield
    if CAPTURE:
        session.config.pluginmanager.getplugin("capturemanager").resume_global_capture()
        os.write(2, b"ordinary-stderr-is-captured\\n")
    diagnostics._mark("blocked-hook-enter")
    blocked_sessionfinish()

def blocked_sessionfinish():
    threading.Event().wait()
'''.replace("CAPTURE", repr(mode == "captured-stderr")), encoding="utf-8")
    env = build_subprocess_env()
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["TMPDIR"] = str(tmp_path)
    proc, _ = launch.start_pytest(
        test, ["-p", PLUGIN, "-o", "exit_diagnostics_delay=0.1", "-q"],
        ROOT, env, str(tmp_path),
    )
    captured = []
    stacks_seen = threading.Event()
    required = 2 if mode == "repeat" else 1

    def read_output():
        stacks = 0
        for line in proc.stdout:
            captured.append(line)
            if "in blocked_sessionfinish" in line:
                stacks += 1
                if stacks >= required:
                    stacks_seen.set()

    reader = threading.Thread(target=read_output)
    reader.start()
    try:
        assert stacks_seen.wait(15), "".join(captured)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)
        reader.join(timeout=10)
        proc.stdout.close()
    assert not reader.is_alive()
    output = "".join(captured)
    assert "blocked-hook-enter" in output, output
    assert output.count("Timeout (") >= required, output
    assert "unconfigure-enter" not in output, output
    assert "ordinary-stderr-is-captured" not in output, output


@pytest.mark.parametrize("succeeds", [True, False])
def test_plugin_preserves_normal_pytest_exit(tmp_path, monkeypatch, succeeds):
    monkeypatch.setattr(launch, "_fork_server_for_run", lambda root: None)
    test = tmp_path / "test_result.py"
    test.write_text(f"def test_result():\n    assert {succeeds!r}\n", encoding="utf-8")
    env = build_subprocess_env()
    env["HERMES_HOME"] = str(tmp_path / "home")
    proc, _ = launch.start_pytest(test, ["-p", PLUGIN, "-q"], ROOT, env, str(tmp_path))
    try:
        output, _ = proc.communicate(timeout=30)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert proc.returncode == (0 if succeeds else 1), output
    assert "atexit-enter" in output
    assert "Timeout (" not in output


@pytest.mark.platforms("linux")
def test_forkserver_diagnostics_preserve_success_and_poll(tmp_path, monkeypatch):
    server = launch._start_fork_server(ROOT)
    monkeypatch.setattr(launch, "_fork_server_for_run", lambda root: server)
    test = tmp_path / "test_result.py"
    test.write_text("def test_result():\n    assert True\n", encoding="utf-8")
    env = build_subprocess_env()
    env["HERMES_HOME"] = str(tmp_path / "home")
    proc, _ = launch.start_pytest(test, ["-p", PLUGIN, "-q"], ROOT, env, str(tmp_path))
    try:
        output, _ = proc.communicate(timeout=30)
        assert proc.poll() == 0
        assert "1 passed" in output
        assert "unconfigure-return" in output
    finally:
        proc.kill()
        launch.forked_exit_status(proc._proc, 10)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("name", ["test_derived_session_after_delete.py", "test_authenticated_session_identity.py", "test_other.py"])
def test_launcher_instruments_only_affected_file(tmp_path, monkeypatch, name):
    monkeypatch.setattr(launch, "_fork_server_for_run", lambda root: None)
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    monkeypatch.setattr(launch.os, "getpgid", lambda pid: pid)
    affected = name != "test_other.py"
    file = tmp_path / "tests/gateway" / name
    args = ["-q"]
    launch.start_pytest(file, args, tmp_path, {}, str(tmp_path))
    assert (PLUGIN in popen.call_args.args[0]) is affected
    assert args == ["-q"]  # retries must not accumulate injected plugins


@pytest.mark.parametrize("status", [None, 0])
def test_forked_child_poll_observes_status_without_consuming_output(monkeypatch, status):
    child = launch._ForkedPytest.__new__(launch._ForkedPytest)
    child._proc = object()
    probe = Mock(return_value=status)
    monkeypatch.setattr(launch, "forked_exit_status", probe)
    assert child.poll() == status
    assert child.returncode == status
    probe.assert_called_once_with(child._proc, 0)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("status", [0, 1, -9])
@pytest.mark.parametrize("poll_first", [False, True])
def test_forked_status_survives_consumed_sentinel(tmp_path, monkeypatch, status, poll_first):
    import os
    from multiprocessing import process as mp_process
    from multiprocessing.connection import wait
    from multiprocessing.forkserver import write_signed

    # Real multiprocessing status decoding, with the forkserver's writer held
    # open after its status write. No EOF can mask the consumed-pipe race.
    read_fd, write_fd = os.pipe()
    proc = _fake_forkserver_child(read_fd)
    child = launch._ForkedPytest.__new__(launch._ForkedPytest)
    child._proc = proc
    child.returncode = None
    child.pid = 123
    child._out = tmp_path / "output"
    child._out.write_text("preserved diagnostics", encoding="utf-8")
    kill = Mock()
    monkeypatch.setattr(launch.os, "kill", kill)
    try:
        assert child.poll() is None  # a live child still has no status
        with pytest.raises(subprocess.TimeoutExpired):
            child.communicate(timeout=0)
        assert child._out.exists()
        write_signed(write_fd, status)
        if poll_first:
            assert child.poll() == status
        assert child.communicate(timeout=0) == ("preserved diagnostics", None)
        # Every status return joins: the finished child leaves multiprocessing's
        # registry instead of lingering for the next Process.start to poll.
        assert proc not in mp_process._children
        assert wait([read_fd], 0) == []  # consumed bytes, writer not closed
        assert child.poll() == status
        assert child.poll() == status
        assert child.returncode == status
        assert not child._out.exists()
        child.kill()
        kill.assert_not_called()  # terminal status must not resurrect kill eligibility
    finally:
        mp_process._children.discard(proc)
        os.close(write_fd)
        os.close(read_fd)


def _fake_forkserver_child(read_fd):
    """A started-looking forkserver Process whose status pipe the test owns.

    Real Popen.poll/read_signed decoding; registered in multiprocessing's child
    registry as Process.start does, so Process.start's _cleanup() polls it too.
    """
    import multiprocessing
    from multiprocessing import process as mp_process
    from multiprocessing.popen_forkserver import Popen

    popen = Popen.__new__(Popen)
    popen.sentinel = read_fd
    popen.returncode = None
    proc = multiprocessing.get_context("forkserver").Process()
    proc._popen = popen
    proc._sentinel = read_fd
    mp_process._children.add(proc)
    return proc


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("status", [0, 1, -9])
@pytest.mark.parametrize("written", ["before_call", "during_wait"])
def test_forked_status_read_excludes_concurrent_process_start(monkeypatch, status, written):
    """A Process.start racing the status read must not consume the status bytes.

    Interleaving, made deterministic: the status poll has seen the sentinel
    readable and is about to read the 8 status bytes when another thread runs
    start_forked(), whose Process.start() first polls every registered child
    (multiprocessing.process._cleanup). Unlocked, that poll reads the bytes and
    the status poll then reads EOF, which multiprocessing reports as 255.

    before_call: the status is already written, so the early exitcode read
    races. during_wait: the early exitcode read finds nothing, the status
    arrives while forked_exit_status waits on the sentinel, and the race hits
    the post-wait join()/exitcode read.
    """
    import os
    from multiprocessing import connection, forkserver
    from multiprocessing import process as mp_process

    read_fd, write_fd = os.pipe()
    proc = _fake_forkserver_child(read_fd)
    status_reader = threading.get_ident()
    waits = []  # sentinel wait timeouts on the status-reading thread
    writer_open = [True]

    def write_status_and_close():
        forkserver.write_signed(write_fd, status)
        os.close(write_fd)  # the server has closed its writer: a lost read sees EOF
        writer_open.clear()

    if written == "before_call":
        write_status_and_close()
    else:
        real_wait = connection.wait

        def wait(objects, timeout=None):
            if threading.get_ident() == status_reader:
                if not waits:
                    assert real_wait(objects, 0) == []  # no status yet
                elif len(waits) == 1:  # forked_exit_status now waits on the sentinel
                    write_status_and_close()
                waits.append(timeout)
            return real_wait(objects, timeout)

        monkeypatch.setattr(connection, "wait", wait)

    decided = threading.Event()  # the racing start either waits on the lock or has polled
    contended = threading.Event()
    starter_done = threading.Event()

    class ObservedLock:
        """_fork_lock that records when a caller has to wait for it."""

        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if not self._lock.acquire(blocking=False):
                contended.set()
                decided.set()
                self._lock.acquire()
            return self

        def __exit__(self, *exc):
            self._lock.release()

    class RacingStart:
        def start(self):  # what Process.start runs before forking
            mp_process._cleanup()
            starter_done.set()
            decided.set()

    monkeypatch.setattr(launch, "_fork_lock", ObservedLock())
    real_read = forkserver.read_signed
    status_reader = threading.get_ident()
    racer = threading.Thread(target=launch.start_forked, args=(RacingStart(),), daemon=True)
    raced = []

    def read_signed(fd):
        if threading.get_ident() == status_reader and not raced:
            raced.append(True)
            racer.start()  # the status poll is between wait() and its read
            assert decided.wait(30), "racing Process.start neither waited nor polled"
        return real_read(fd)

    monkeypatch.setattr(forkserver, "read_signed", read_signed)
    try:
        assert launch.forked_exit_status(proc, 30) == status
        racer.join(30)
        assert not racer.is_alive()
        assert contended.is_set()  # the start waited for the status read to finish
        assert starter_done.is_set()
        assert proc.exitcode == status
        assert proc not in mp_process._children
        if written == "during_wait":  # early exitcode poll, then the sentinel wait
            assert waits[:2] == [0, 30]
    finally:
        mp_process._children.discard(proc)
        if writer_open:
            os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("status", [None, 0])
def test_runner_records_child_status_before_killing(tmp_path, monkeypatch, status):
    from scripts import run_tests_parallel as runner

    proc = Mock()
    proc.communicate.side_effect = [subprocess.TimeoutExpired("pytest", 2), ("3 passed in 0.1s", None)]
    proc.poll.return_value = status
    monkeypatch.setattr(runner, "start_pytest", lambda *args, **kw: (proc, 123))
    monkeypatch.setattr(runner, "child_environment", lambda *args: None)
    monkeypatch.setattr(runner, "_runner_scratch_root", lambda: str(tmp_path))
    monkeypatch.setattr(runner._rss, "reap_with_rusage", lambda p: None)
    monkeypatch.setattr(runner._rss, "record_peak_rss", lambda *args: None)
    monkeypatch.setattr(runner._rss, "proc_peak_rss_bytes", lambda p: 0)
    monkeypatch.setattr(runner, "_kill_detached_leftovers", lambda root: None)

    def kill(p, **kwargs):
        p.poll.assert_called_once_with()
        p.poll.return_value = -9  # Reporting after the kill would lose the useful state.

    monkeypatch.setattr(runner, "_kill_tree", kill)
    _, rc, output, summary, _ = runner._run_one_file_attempt(tmp_path / "test_pass.py", [], tmp_path, 2)
    assert rc == 124
    assert summary["passed"] == 3
    assert f"child exit status before kill={status!r}" in output
