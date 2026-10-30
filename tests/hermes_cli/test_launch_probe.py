"""Behaviour contract of hermes_cli.launch_probe: bounded, passive, no leaked processes."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import launch_probe
from tests.waiting import wait_until

posix = pytest.mark.platforms("linux", "macos")
BOUND = 3.0

_HOLDER = (
    "import subprocess, sys\n"
    "def hold(pid_file):\n"
    "    child = subprocess.Popen([sys.executable, '-c',\n"
    "        'import threading; threading.Event().wait()', 'launch-probe-sentinel'])\n"
    "    open(pid_file, 'w').write(str(child.pid))\n"
)


def _fake_source(tmp_path: Path, venv_is_current: str, activate_dependencies: str,
                 activate: str = "return []") -> Path:
    source = tmp_path / "source"
    (source / "pm").mkdir(parents=True)
    (source / "holder.py").write_text(_HOLDER)
    (source / "pm" / "__init__.py").write_text(
        "def venv_is_current(*, project_root):\n    import holder, os\n"
        f"    {venv_is_current}\n"
        "def activate():\n    import os\n"
        f"    {activate}\n"
    )
    (source / "pm" / "environments.py").write_text(
        "def activate_dependencies(root):\n    import holder, os\n"
        f"    {activate_dependencies}\n"
    )
    return source


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _pid(pid_file: Path) -> int:
    wait_until(pid_file.exists, timeout=5, what="descendant pid file")
    wait_until(lambda: pid_file.read_text() != "", timeout=5, what="descendant pid")
    return int(pid_file.read_text())


@pytest.fixture
def use_source(monkeypatch):
    def use(source: Path) -> None:
        monkeypatch.setattr(launch_probe, "SOURCE_ROOT", source)
    return use


@posix
@pytest.mark.parametrize("current", [True, False])
def test_currency_reports_pm_verdict_for_requested_root(tmp_path, use_source, current):
    seen = tmp_path / "root.txt"
    use_source(_fake_source(
        tmp_path,
        f"open({str(seen)!r}, 'w').write(str(project_root)); return {current}",
        "pass",
    ))
    assert launch_probe.probe(tmp_path / "fixture-root", timeout=BOUND) == {
        "schema": 1, "state": "current" if current else "stale"}
    assert seen.read_text() == str(tmp_path / "fixture-root")


@posix
def test_runtime_activates_dependencies_before_pm_and_returns_path(tmp_path, use_source):
    order = tmp_path / "order.txt"
    use_source(_fake_source(
        tmp_path, "return True",
        f"open({str(order)!r}, 'a').write('deps:' + str(root) + ' ')",
        f"open({str(order)!r}, 'a').write('activate '); os.environ['PATH'] = '/store/bin'; return ['broken']",
    ))
    assert launch_probe.probe(tmp_path / "r", "runtime", timeout=BOUND) == {
        "schema": 1, "state": "runtime", "problems": ["broken"], "path": "/store/bin"}
    assert order.read_text() == f"deps:{tmp_path / 'r'} activate "


@posix
def test_worker_failure_is_an_error_result(tmp_path, use_source):
    use_source(_fake_source(tmp_path, "raise RuntimeError('boom')", "pass"))
    assert launch_probe.probe(tmp_path, timeout=BOUND) == {
        "schema": 1, "state": "error", "detail": "RuntimeError: boom"}


@posix
def test_stdout_noise_from_imports_does_not_corrupt_the_reply(tmp_path, use_source):
    use_source(_fake_source(tmp_path, "print('noise'); return True", "pass"))
    assert launch_probe.probe(tmp_path, timeout=BOUND) == {"schema": 1, "state": "current"}


@posix
@pytest.mark.parametrize("body", [
    "import sys; sys.stdout.write('not json\\n')",
    "import sys; sys.stdout.write('{\"schema\": 2, \"state\": \"current\"}\\n')",
    "import sys; sys.stdout.write('{\"schema\": 1, \"state\": \"runtime\"}\\n')",
    "import sys; sys.stdout.write('x' * 200000)",
    "pass",
])
def test_malformed_worker_answers_are_none(monkeypatch, tmp_path, body):
    real_popen = launch_probe.subprocess.Popen

    def popen(argv, **kwargs):
        return real_popen([sys.executable, "-I", "-S", "-c", body], **kwargs)

    monkeypatch.setattr(launch_probe.subprocess, "Popen", popen)
    assert launch_probe.probe(tmp_path, timeout=BOUND) is None


@posix
def test_invalid_arguments_and_missing_interpreter_are_none(monkeypatch, tmp_path):
    assert launch_probe.probe(tmp_path, "install") is None
    assert launch_probe.probe(tmp_path, timeout=0) is None
    monkeypatch.setattr(launch_probe.sys, "executable", str(tmp_path / "missing-python"))
    assert launch_probe.probe(tmp_path, timeout=BOUND) is None


@posix
def test_blocked_pm_runtime_preparation_times_out_and_leaves_no_descendant(tmp_path, use_source):
    pid_file = tmp_path / "pid"
    use_source(_fake_source(
        tmp_path, "return True",
        f"holder.hold({str(pid_file)!r}); import threading; threading.Event().wait()",
    ))
    start = time.monotonic()
    assert launch_probe.probe(tmp_path, "runtime", timeout=0.5) is None
    assert time.monotonic() - start <= BOUND
    pid = _pid(pid_file)
    wait_until(lambda: not _alive(pid), timeout=BOUND, what="sentinel descendant gone")


@posix
def test_descendant_holding_stdout_after_reply_neither_hangs_nor_survives(tmp_path, use_source):
    pid_file = tmp_path / "pid"
    use_source(_fake_source(tmp_path, f"holder.hold({str(pid_file)!r}); return True", "pass"))
    start = time.monotonic()
    assert launch_probe.probe(tmp_path, timeout=BOUND) == {"schema": 1, "state": "current"}
    assert time.monotonic() - start <= BOUND
    pid = _pid(pid_file)
    wait_until(lambda: not _alive(pid), timeout=BOUND, what="sentinel descendant gone")


@pytest.mark.platforms("windows")
def test_windows_has_no_probe_and_returns_none(tmp_path):
    assert launch_probe.probe(tmp_path) is None
