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


def test_rows_list_each_process_with_command_and_age(home, sleeper):
    """The sidebar badge's tooltip: what the process is and how long it ran."""
    a, b = sleeper(), sleeper()
    (home / "processes.json").write_text(json.dumps([
        {**_entry("proc_new", b.pid, session_key="s_w"), "command": "sleep 600", "started_at": 200.0},
        {**_entry("proc_old", a.pid, session_key="s_w"), "command": "x " * 200, "started_at": 100.0},
    ]))
    rows = [{"id": "s_w"}, {"id": "s_idle", "background_processes": [{"id": "stale"}]}]

    psa.annotate_background_activity(rows)

    procs = rows[0]["background_processes"]
    assert [p["id"] for p in procs] == ["proc_old", "proc_new"]  # oldest first
    assert procs[1] == {"id": "proc_new", "command": "sleep 600", "started_at": 200.0, "pid": b.pid}
    assert len(procs[0]["command"]) <= 160 and procs[0]["command"].endswith("\u2026")
    assert "background_processes" not in rows[1]


def test_stop_kills_checkpoint_process_owned_by_row_only(home, sleeper):
    live = sleeper()
    (home / "processes.json").write_text(json.dumps([_entry("proc_x", live.pid, session_key="s_own")]))

    assert psa.stop_session_process("proc_x", ["s_other"])["status"] == "not_found"
    assert live.poll() is None  # a row can't stop another row's process

    psa.annotate_background_activity([{"id": "s_own"}])  # warm the cache
    assert psa.stop_session_process("proc_x", ["s_own"])["status"] == "killed"
    assert live.wait(timeout=5) is not None
    rows = [{"id": "s_own"}]
    psa.annotate_background_activity(rows)
    assert "background_active" not in rows[0]  # cache dropped: the row goes green at once


def test_stop_uses_registry_kill_for_in_process_sessions(home, sleeper, monkeypatch):
    import tools.process_registry as pr

    live = sleeper()
    pr.process_registry._running["proc_mem"] = ProcessSession(
        id="proc_mem", command="sleep 600", task_id="default", session_key="s_mem", pid=live.pid,
        host_start_time=ProcessRegistry._safe_host_start_time(live.pid), started_at=time.time())
    calls = []
    monkeypatch.setattr(pr.process_registry, "kill_process",
                        lambda pid, source="": calls.append((pid, source)) or {"status": "killed"})

    assert psa.stop_session_process("proc_mem", ["s_mem"]) == {"status": "killed"}
    assert calls == [("proc_mem", "sidebar.stop")]


def test_stop_endpoint(home, sleeper):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    live = sleeper()
    (home / "processes.json").write_text(json.dumps([_entry("proc_e", live.pid, session_key="s_root")]))
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    url = "/api/sessions/s_tip/background-processes/proc_e/stop"

    assert client.post(url, json={}).status_code == 404  # tip alone doesn't own it
    res = client.post(url, json={"session_ids": ["s_root"]})

    assert res.status_code == 200 and res.json()["status"] == "killed"
    assert live.wait(timeout=5) is not None


# Exactly what hermes-mods' kanban-worker-nice terminal_request middleware prefixes on protected Linux hosts.
NICE_PREAMBLE = ('# kanban-worker-nice terminal floor\n'
                 'if [ "$(nice)" -lt 10 ]; then renice -n 10 -p "${BASHPID:-$$}" >/dev/null || exit $?; fi\n')


def test_process_labels_drop_injected_nice_preamble(home, sleeper):
    """Chat 20261127_202849_4c4281: two ``bb-land --wait`` rows read ``# kanban-worker-nice terminal floor if ...``."""
    import tools.process_registry as pr
    from hermes_state_outcome import _process_items

    a, b = sleeper(), sleeper()
    (home / "processes.json").write_text(json.dumps([
        {**_entry("proc_land", a.pid, session_key="s_w"), "command": NICE_PREAMBLE + "bb-land --wait 858",
         "started_at": 100.0},
    ]))
    pr.process_registry._running["proc_mem"] = ProcessSession(
        id="proc_mem", command=NICE_PREAMBLE + "cd /repo && gh pr checks 858 --watch", task_id="default",
        session_key="s_w", pid=b.pid, host_start_time=ProcessRegistry._safe_host_start_time(b.pid), started_at=200.0)
    rows = [{"id": "s_w"}]

    psa.annotate_background_activity(rows)

    assert [p["command"] for p in rows[0]["background_processes"]] == [
        "bb-land --wait 858", "cd /repo && gh pr checks 858 --watch"]
    psa._reset_cache_for_tests()
    assert sorted(i["label"] for i in _process_items("s_w", ["s_w"])) == [
        "bb-land --wait 858", "cd /repo && gh pr checks 858 --watch"]


def test_process_labels_keep_agent_comments_and_unprefixed_commands(home, sleeper):
    a, b, c = sleeper(), sleeper(), sleeper()
    (home / "processes.json").write_text(json.dumps([
        {**_entry("proc_a", a.pid, session_key="s_w"), "command": "# my note\nsleep 600", "started_at": 1.0},
        {**_entry("proc_b", b.pid, session_key="s_w"), "command": "echo '# kanban-worker-nice terminal floor'",
         "started_at": 2.0},
        {**_entry("proc_c", c.pid, session_key="s_w"), "command": NICE_PREAMBLE, "started_at": 3.0},
    ]))
    rows = [{"id": "s_w"}]

    psa.annotate_background_activity(rows)

    assert [p["command"] for p in rows[0]["background_processes"]] == [
        "# my note sleep 600", "echo '# kanban-worker-nice terminal floor'", "background process"]


def test_foreign_checkpoint_outcome_label_drops_injected_nice_preamble(home, sleeper):
    """refresh_outcome's foreign-checkpoint items label the same way as the sidebar's."""
    from hermes_state_process_checkpoint import checkpoint_items

    live = sleeper()
    (home / "processes.json").write_text(json.dumps([
        {**_entry("proc_land", live.pid, parent_session_id="s_w"), "command": NICE_PREAMBLE + "bb-land --wait 858"},
        {**_entry("proc_bare", live.pid, parent_session_id="s_w"), "command": NICE_PREAMBLE},
    ]))
    registry = type("R", (), {"_detached_host_fate": staticmethod(lambda pid, birth: "running")})

    assert [i["label"] for i in checkpoint_items(registry, "s_w", set())] == ["bb-land --wait 858", "proc_bare"]
