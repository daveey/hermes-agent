"""Desktop/TUI backend re-adopts its orphaned background jobs after a restart. [daveey]

A Desktop chat's ``notify_on_complete`` job outlives a Desktop restart (the
backend that spawned it is gone). The new backend must adopt it again, watch its
PID, and wake the chat when it exits, with an honest "status unknown" since no
pipe is left to collect the exit code. The messaging gateway must neither adopt
it nor kill it on shutdown.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.process_registry import ProcessRegistry
from tools.process_registry_notifications import format_process_notification, process_completion_display_text
from tools.process_registry_readopt import (
    UNKNOWN_EXIT_STATUS, checkpoint_entry_owner, recover_tui_orphaned_processes,
)

TUI_KEY = "20261014_152627_e39461"
GW_KEY = "agent:main:telegram:dm:42"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _entry(sid: str, pid: int, key: str, *, notify=True, platform="") -> dict:
    return {
        "session_id": sid, "command": "bash watch_onit.sh", "pid": pid, "pid_scope": "host",
        "host_start_time": ProcessRegistry._safe_host_start_time(pid), "started_at": time.time(),
        "task_id": "default", "owner_task_id": key, "session_key": key, "notify_on_complete": notify,
        "watcher_platform": platform, "writer_pid": _dead_pid(), "writer_start_time": None,
    }


@pytest.fixture(autouse=True)
def _desktop_backend(monkeypatch):
    """These tests play the Desktop-owned backend; only it adopts (a `hermes dashboard` never does)."""
    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda argv=None: True)


@pytest.fixture()
def procs():
    started: list[subprocess.Popen] = []

    def spawn() -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
        started.append(proc)
        return proc

    yield spawn
    for proc in started:
        proc.kill()
        proc.wait()


@pytest.fixture()
def checkpoint(tmp_path):
    path = tmp_path / "processes.json"
    with patch("tools.process_registry.CHECKPOINT_PATH", path):
        yield path


def _drain(registry: ProcessRegistry) -> list[dict]:
    out = []
    while not registry.completion_queue.empty():
        out.append(registry.completion_queue.get_nowait())
    return out


def _kill(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait()


def test_entry_owner_classification():
    assert checkpoint_entry_owner({"session_key": TUI_KEY}) == "tui"
    assert checkpoint_entry_owner({"session_key": GW_KEY}) == "gateway"
    assert checkpoint_entry_owner({"session_key": TUI_KEY, "watcher_platform": "slack"}) == "gateway"
    assert checkpoint_entry_owner({"session_key": ""}) == ""


def test_tui_recovery_adopts_only_desktop_orphans(checkpoint, procs):
    checkpoint.write_text(json.dumps([
        _entry("proc_tui", procs().pid, TUI_KEY),
        _entry("proc_gw", procs().pid, GW_KEY),
        _entry("proc_keyless", procs().pid, ""),
    ]))
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint(owner="tui") == 1
    assert set(registry._running) == {"proc_tui"}
    # The other kinds stay in the shared file for their own owner.
    assert {e["session_id"] for e in json.loads(checkpoint.read_text())} == {"proc_tui", "proc_gw", "proc_keyless"}


def test_gateway_recovery_leaves_desktop_orphans_alone(checkpoint, procs):
    checkpoint.write_text(json.dumps([
        _entry("proc_tui", procs().pid, TUI_KEY),
        _entry("proc_gw", procs().pid, GW_KEY),
        _entry("proc_keyless", procs().pid, ""),
    ]))
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint(owner="gateway") == 2
    assert set(registry._running) == {"proc_gw", "proc_keyless"}


def test_gateway_shutdown_kill_all_spares_desktop_orphan(checkpoint, procs):
    """Item 3: the gateway never adopts a Desktop chat's job, so its shutdown sweep cannot kill it."""
    desktop_child = procs()
    checkpoint.write_text(json.dumps([_entry("proc_tui", desktop_child.pid, TUI_KEY)]))
    registry = ProcessRegistry()
    registry.recover_from_checkpoint(owner="gateway")
    registry.kill_all(source="gateway_shutdown")
    time.sleep(0.2)
    assert desktop_child.poll() is None, "the Desktop chat's job survived the gateway shutdown"


def test_readopted_notify_job_exit_queues_status_unknown(checkpoint, procs):
    child = procs()
    checkpoint.write_text(json.dumps([_entry("proc_4fa0a87e0e10", child.pid, TUI_KEY)]))
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint(owner="tui") == 1
    assert registry.sweep_detached_once() == 1
    assert _drain(registry) == []

    _kill(child)
    assert registry.sweep_detached_once() == 0
    (event,) = _drain(registry)
    assert event["type"] == "completion"
    assert event["session_key"] == TUI_KEY
    assert event["session_id"] == "proc_4fa0a87e0e10"
    assert event["exit_code"] is None
    text = format_process_notification(event)
    assert UNKNOWN_EXIT_STATUS in text
    assert "exit code ?" in text
    assert "status unknown" in process_completion_display_text([event]).lower()
    # Swept once: a second sweep (or any refresh) does not re-notify.
    registry.sweep_detached_once()
    assert registry.list_sessions() == []
    assert _drain(registry) == []


def test_readopted_job_without_notify_exits_silently(checkpoint, procs):
    child = procs()
    checkpoint.write_text(json.dumps([_entry("proc_quiet", child.pid, TUI_KEY, notify=False)]))
    registry = ProcessRegistry()
    registry.recover_from_checkpoint(owner="tui")
    _kill(child)
    registry.sweep_detached_once()
    assert _drain(registry) == []


def test_gateway_recovered_job_keeps_existing_silent_prune(checkpoint, procs):
    """The gateway has its own watcher delivery; the unknown-exit completion is TUI-only."""
    child = procs()
    checkpoint.write_text(json.dumps([_entry("proc_gw", child.pid, GW_KEY)]))
    registry = ProcessRegistry()
    registry.recover_from_checkpoint(owner="gateway")
    _kill(child)
    registry.sweep_detached_once()
    assert _drain(registry) == []


def test_orphan_that_died_while_backend_was_down_still_notifies(checkpoint):
    checkpoint.write_text(json.dumps([_entry("proc_died", _dead_pid(), TUI_KEY)]))
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint(owner="tui") == 0
    (event,) = _drain(registry)
    assert event["session_id"] == "proc_died" and event["completion_reason"] == "unknown"
    assert json.loads(checkpoint.read_text()) == []


def test_startup_helper_adopts_and_sweeps_until_exit(checkpoint, procs):
    child = procs()
    checkpoint.write_text(json.dumps([_entry("proc_sweep", child.pid, TUI_KEY)]))
    registry = ProcessRegistry()
    assert recover_tui_orphaned_processes(registry, sweep_interval=0.05) == 1
    _kill(child)
    deadline = time.monotonic() + 5
    events: list = []
    while not events and time.monotonic() < deadline:
        time.sleep(0.05)
        events = _drain(registry)
    assert [e["session_id"] for e in events] == ["proc_sweep"]
    deadline = time.monotonic() + 2
    while registry._detached_sweep_thread is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert registry._detached_sweep_thread is None, "the sweep thread ends once nothing is left to watch"


def test_adopter_stamps_itself_as_writer_so_a_second_backend_skips(checkpoint, procs):
    """Recovery writes under the file lock it read under, so the next backend sees a live writer."""
    import os

    checkpoint.write_text(json.dumps([_entry("proc_once", procs().pid, TUI_KEY)]))
    assert ProcessRegistry().recover_from_checkpoint(owner="tui") == 1
    (entry,) = json.loads(checkpoint.read_text())
    assert entry["writer_pid"] == os.getpid()


def test_concurrent_backends_adopt_an_orphan_once(checkpoint, procs, tmp_path):
    child = procs()
    checkpoint.write_text(json.dumps([_entry("proc_once", child.pid, TUI_KEY)]))
    script = tmp_path / "adopt.py"
    script.write_text(
        "import sys, time\n"
        "from unittest.mock import patch\n"
        "from tools.process_registry import ProcessRegistry\n"
        f"with patch('tools.process_registry.CHECKPOINT_PATH', __import__('pathlib').Path({str(checkpoint)!r})):\n"
        "    print(ProcessRegistry().recover_from_checkpoint(owner='tui'), flush=True)\n"
        "    time.sleep(10)\n")  # stay alive: a live writer is what makes the sibling skip
    import os
    # `python script.py` puts the script's dir (tmp_path), not the cwd, on sys.path, so the repo
    # root must be passed explicitly or `import tools...` fails wherever the repo isn't installed
    # editable (CI), leaving empty stdout and an IndexError instead of the real error.
    repo_root = str(Path(__file__).resolve().parents[2])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [repo_root, env.get("PYTHONPATH")]))
    runs = [subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=repo_root, env=env) for _ in range(4)]
    outs = [run.communicate(timeout=60) for run in runs]
    for run, (out, err) in zip(runs, outs):
        assert run.returncode == 0 and out.strip(), f"adopter subprocess failed ({run.returncode}): {err}"
    counts = [int(out.strip().splitlines()[-1]) for out, _ in outs]
    assert sorted(counts) == [0, 0, 0, 1]


def test_parked_events_are_taken_only_by_their_owner():
    registry = ProcessRegistry()
    mine, theirs = {"session_key": "a"}, {"session_key": "b"}
    registry.park_unowned_event(mine)
    registry.park_unowned_event(theirs)
    from tools.process_registry_awaiting import release
    taken = registry.take_parked_events(lambda e: e["session_key"] == "a")
    try:
        assert taken == [mine]
        assert registry.take_parked_events(lambda e: e["session_key"] == "a") == []
    finally:
        release(taken)
    taken = registry.take_parked_events(lambda e: True)
    try:
        assert taken == [theirs]
    finally:
        release(taken)


def test_start_tui_orphan_recovery_runs_off_thread(checkpoint, procs):
    from tools.process_registry_readopt import start_tui_orphan_recovery

    checkpoint.write_text(json.dumps([_entry("proc_bg", procs().pid, TUI_KEY)]))
    registry = ProcessRegistry()
    start_tui_orphan_recovery(registry).join(10)
    assert set(registry._running) == {"proc_bg"}


def test_messaging_gateway_and_desktop_backend_recover_with_their_owner_filter():
    """Both startup paths name their owner, so neither adopts (or later kill_all's) the other's jobs."""
    import inspect

    from gateway import run_startup
    # Fork: upstream split FastAPI app assembly (incl. the startup hook) out of web_server into web_server_app.
    from hermes_cli import web_server_app

    gw = inspect.getsource(run_startup)
    assert "recover_from_checkpoint()" not in gw
    assert gw.count('recover_from_checkpoint(owner="gateway")') == 2
    assert "start_tui_orphan_recovery()" in inspect.getsource(web_server_app)


def test_reassert_checkpoint_restores_entries_a_foreign_writer_dropped(checkpoint, procs):
    """A pre-update Hermes process overwrites processes.json with only its own entries; the
    Desktop backend's keeper must put its live watcher back so a later restart re-adopts it. [daveey]"""
    checkpoint.write_text(json.dumps([_entry("proc_tui", procs().pid, TUI_KEY)]))
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint(owner="tui") == 1
    assert registry.reassert_checkpoint() is False  # nothing missing: no rewrite
    checkpoint.write_text(json.dumps([{"session_id": "proc_foreign", "pid": 1, "pid_scope": "host"}]))
    assert registry.reassert_checkpoint() is True
    assert "proc_tui" in {e["session_id"] for e in json.loads(checkpoint.read_text())}
    assert registry.reassert_checkpoint() is False


def test_reassert_checkpoint_noop_without_live_sessions(checkpoint):
    checkpoint.write_text("[]")
    assert ProcessRegistry().reassert_checkpoint() is False
    assert checkpoint.read_text() == "[]"
