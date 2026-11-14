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
@pytest.mark.parametrize("affected", [True, False])
def test_launcher_instruments_only_affected_file(tmp_path, monkeypatch, affected):
    monkeypatch.setattr(launch, "_fork_server_for_run", lambda root: None)
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    monkeypatch.setattr(launch.os, "getpgid", lambda pid: pid)
    file = tmp_path / ("tests/gateway/test_derived_session_after_delete.py" if affected else "test_other.py")
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
