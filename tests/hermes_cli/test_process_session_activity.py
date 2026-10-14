"""background_active stamping on session-list rows (sidebar 'still working' state). [daveey]"""

import json
import subprocess
import sys
import time

import pytest

from hermes_cli import process_session_activity as psa
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "process_registry", ProcessRegistry())
    psa._reset_cache_for_tests()
    yield tmp_path
    psa._reset_cache_for_tests()


@pytest.fixture
def sleeper():
    procs = []

    def spawn():
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        procs.append(proc)
        return proc

    yield spawn
    for proc in procs:
        proc.kill()
        proc.wait()


def _entry(sid, pid, **owner):
    return {"session_id": sid, "pid": pid, "pid_scope": "host",
            "host_start_time": ProcessRegistry._safe_host_start_time(pid), "task_id": "default", **owner}


def test_checkpoint_process_marks_owning_session_busy(home, sleeper):
    """The 20261014_161613_1af8dd case: ``gh pr checks --watch`` registered in
    processes.json under the session, the renderer never polled it."""
    live = sleeper()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (home / "processes.json").write_text(json.dumps([
        _entry("proc_114c3e5a186f", live.pid, session_key="s_watch", owner_task_id="s_watch"),
        _entry("proc_dead", dead.pid, session_key="s_dead"),
    ]))
    rows = [{"id": "s_watch"}, {"id": "s_dead", "background_active": 1}, {"id": "s_idle"}]

    psa.annotate_background_activity(rows)

    assert rows[0]["background_active"] == 1
    assert "background_active" not in rows[1]  # pid gone: not working; stale stamp cleared
    assert "background_active" not in rows[2]


def test_lineage_and_session_task_ids_count_each_process_once(home, sleeper):
    live = sleeper()
    (home / "processes.json").write_text(json.dumps([
        _entry("proc_a", live.pid, session_key="s_root", parent_session_id="s_tip"),
        _entry("proc_b", sleeper().pid, task_id="session:s_root"),
    ]))
    rows = [{"id": "s_tip", "_lineage_root_id": "s_root", "_lineage_ids": ["s_root", "s_tip"]}]

    psa.annotate_background_activity(rows)

    assert rows[0]["background_active"] == 2


def test_in_process_registry_counts(home, sleeper):
    import tools.process_registry as pr

    live = sleeper()
    pr.process_registry._running["proc_mem"] = ProcessSession(
        id="proc_mem", command="x", task_id="default", session_key="s_mem", pid=live.pid,
        host_start_time=ProcessRegistry._safe_host_start_time(live.pid), started_at=time.time())

    assert psa.background_process_counts() == {"s_mem": 1}
