"""Restart-adoption matrix for persisted background jobs. [daveey]

A ``persist_on_release`` job outlives the interpreter that spawned it (a gateway or Desktop backend
restarted by every core deploy). Every case below uses a REAL spawner interpreter that exits right
after the spawn, so the job is settled through adoption only (no Popen): what the adopter reads is
exactly what the job's recording shell left behind.

A. Exit status: whatever the command does to its own shell (``exec``, its own EXIT trap, a heredoc),
   the adopter reports the command's real exit code; a job killed by a signal is reported as
   "status unknown", never as lost (-1, "the process backend disappeared") and never as a false 0.
B. Reachability: the owning chat's ``process_manage`` poll/log/wait/kill find the live job while a
   DIFFERENT live Hermes process supervises it (on the hub the gateway hosts the chat while the
   Desktop-owned backend re-adopts its jobs: proc_1372c59c5218 answered not_found while alive).
"""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time

import pytest

from tools.process_registry import ProcessRegistry, _handle_process
from tools.process_registry_notifications import format_process_notification
from tests.tools import test_process_orphan_readopt_tui as helpers

TUI_KEY = helpers.TUI_KEY
checkpoint = helpers.checkpoint
ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="the persisted exit recorder is POSIX-only"),
    # The orphaned job is reparented away from the test's subtree (its spawner exited, as after a
    # restart); signalling it is the point of these tests and it never leaves this test's own job.
    pytest.mark.live_system_guard_bypass,
]

_SPAWNER = '''
import json, os, sys
from pathlib import Path
os.environ["HERMES_SESSION_ID"] = sys.argv[4]
import tools.process_registry as pr
from tools import process_registry_output_log as logs
pr.CHECKPOINT_PATH = Path(sys.argv[1])
logs.output_dir = lambda: Path(sys.argv[2])
registry = pr.ProcessRegistry()
session = registry.spawn_local(sys.argv[3], cwd=sys.argv[2], session_key=sys.argv[4],
                              owner_task_id=sys.argv[4], persist_on_release=True)
session.notify_on_complete = True
registry._write_checkpoint()
print(json.dumps(dict(id=session.id, pid=session.pid, log=session.output_log)), flush=True)
os._exit(0)
'''

# A second live Hermes process that re-adopts the orphan and keeps supervising it until told to stop.
_ADOPTER = '''
import json, sys
from pathlib import Path
import tools.process_registry as pr
from tools import process_registry_output_log as logs
from tools.process_registry_notifications import format_process_notification
pr.CHECKPOINT_PATH = Path(sys.argv[1])
logs.output_dir = lambda: Path(sys.argv[2])
registry = pr.ProcessRegistry()
events = []
registry.on_unknown_exit = events.append
print(json.dumps({"adopted": registry.recover_from_checkpoint(owner="tui")}), flush=True)
sys.stdin.readline()
session = registry.get(sys.argv[3])
done = session is not None and session._completion_event.wait(20)
print(json.dumps({"done": bool(done), "events": [
    {k: e.get(k) for k in ("session_id", "exit_code", "completion_reason")} for e in events],
    "sources": [e.get("termination_source") for e in events],
    "notices": [format_process_notification(e) for e in events]}), flush=True)
'''


def _gated(gate: Path, body: str) -> str:
    """Wait (bounded) for *gate*, then run *body*: the job is still running when the adopter attaches."""
    return (f'printf "started\\n"; n=0; while [ ! -f {shlex.quote(str(gate))} ]; do '
            'n=$((n+1)); [ "$n" -lt 1500 ] || exit 124; sleep 0.02; done\n' + body)


def _spawn_orphan(checkpoint_path: Path, tmp_path: Path, command: str) -> dict:
    from tools.environments.local import build_subprocess_env
    producer = subprocess.run(
        [sys.executable, "-c", _SPAWNER, str(checkpoint_path), str(tmp_path), command, TUI_KEY],
        cwd=str(ROOT), env=build_subprocess_env(), capture_output=True, text=True, encoding="utf-8",
        timeout=20, check=True)
    job = json.loads(producer.stdout.strip().splitlines()[-1])
    job["entry"] = json.loads(checkpoint_path.read_text())[0]
    return job


def _wait_gone(registry: ProcessRegistry, job: dict, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while registry._detached_host_fate(job["pid"], job["entry"]["host_start_time"]) == "running":
        assert time.monotonic() < deadline, "job never exited"
        threading.Event().wait(0.02)


def _reap(registry: ProcessRegistry, job: dict) -> None:
    if registry._detached_host_fate(job["pid"], job["entry"]["host_start_time"]) == "running":
        os.killpg(job["pid"], signal.SIGKILL)


# ----- A. exit status after adoption -----

_CASES = {
    # The hub's watcher did ``chmod +x w.sh && exec w.sh``: the exec replaced the shell that held the
    # recorder, so its exit 0 settled as lost (-1).
    "exec": ("exec sh -c 'exit 3'", 3),
    "plain": ("printf 'plain\\n'; exit 4", 4),
    "heredoc": ("sh <<'EOF'\nprintf 'heredoc\\n'\nexit 5\nEOF", 5),
    # A command's own EXIT trap replaced the recorder's.
    "own_exit_trap": ("trap 'echo bye' EXIT; exit 6", 6),
    # A parse error killed the recording shell itself (the inline compound was parsed whole before
    # it ran), so nothing was recorded and the job settled as unknown instead of bash's 2.
    "syntax_error": ("printf 'before\\n'; if then", 2),
    # Unterminated: it ate the compound's closing paren (unknown). Unwrapped, bash warns and runs it.
    "unterminated_heredoc": ("sh <<EOF\nexit 5", 5),
}


@pytest.mark.parametrize("case", ["syntax_error", "unterminated_heredoc"])
def test_parse_failure_cases_match_the_unwrapped_command(case):
    """The expected codes are what the same text exits with under a plain ``bash -c``."""
    body, expected = _CASES[case]
    assert subprocess.run(["bash", "-c", body], capture_output=True, timeout=10, check=False).returncode == expected


@pytest.mark.parametrize("exit_while_down", [False, True])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_adopted_job_reports_its_real_exit_code(checkpoint, tmp_path, monkeypatch, case, exit_while_down):
    body, expected = _CASES[case]
    gate = tmp_path / "release"
    monkeypatch.setattr("tools.process_registry_output_log.output_dir", lambda: tmp_path)
    job = _spawn_orphan(checkpoint, tmp_path, _gated(gate, body))
    registry = ProcessRegistry()
    delivered = []
    registry.on_unknown_exit = delivered.append
    session = None
    try:
        assert not registry._writer_alive(job["entry"]), "the spawner must really be gone"
        if exit_while_down:
            gate.touch()
            _wait_gone(registry, job)
        assert registry.recover_from_checkpoint(owner="tui") == 1
        session = registry.get(job["id"])
        gate.touch()
        assert session._completion_event.wait(10)
        assert (session.exit_code, session.completion_reason) == (expected, "exited")
        assert [(e["exit_code"], e["completion_reason"]) for e in delivered] == [(expected, "exited")]
    finally:
        gate.touch()
        if session and session._reader_thread:
            session._reader_thread.join(5)
        _reap(registry, job)


@pytest.mark.parametrize("body", ["sleep 30", "exec sleep 30"])
def test_signalled_job_reports_status_unknown_not_lost_or_zero(checkpoint, tmp_path, monkeypatch, body):
    """A SIGTERM to the job records no status: the notice says unknown. Before, the shared-shell EXIT
    trap wrote ``$?`` = 0 for a plain command (false success) and nothing for an exec (lost, -1)."""
    gate = tmp_path / "release"
    gate.touch()
    monkeypatch.setattr("tools.process_registry_output_log.output_dir", lambda: tmp_path)
    job = _spawn_orphan(checkpoint, tmp_path, _gated(gate, body))
    registry = ProcessRegistry()
    delivered = []
    registry.on_unknown_exit = delivered.append
    session = None
    try:
        assert registry.recover_from_checkpoint(owner="tui") == 1
        session = registry.get(job["id"])
        # Signal only once the command runs: the ``bash -lic`` launcher is interactive and ignores SIGTERM.
        deadline = time.monotonic() + 10
        while "started" not in Path(job["log"]).read_text():
            assert time.monotonic() < deadline, "job never started"
            threading.Event().wait(0.02)
        os.killpg(job["pid"], signal.SIGTERM)
        assert session._completion_event.wait(10)
        assert session.exit_code is None and session.completion_reason == "unknown"
        assert len(delivered) == 1
        text = format_process_notification(delivered[0])
        assert "status unknown" in text
        assert "backend disappeared" not in text and "exit code 0" not in text
        # The settled job stays visible to its own chat's poll (not pruned as an uncollected detach).
        monkeypatch.setenv("HERMES_SESSION_ID", TUI_KEY)
        assert registry.poll(job["id"])["status"] == "exited"
    finally:
        if session and session._reader_thread:
            session._reader_thread.join(5)
        _reap(registry, job)


# ----- B. the owner chat reaches a job another live process supervises -----

@pytest.fixture()
def adopted_elsewhere(checkpoint, tmp_path, monkeypatch):
    """An orphan re-adopted by a separate live interpreter; yields (job, adopter, gate, registry)."""
    from tools.environments.local import build_subprocess_env
    import tools.process_registry as pr
    gate = tmp_path / "release"
    monkeypatch.setattr("tools.process_registry_output_log.output_dir", lambda: tmp_path)
    job = _spawn_orphan(checkpoint, tmp_path, _gated(gate, "printf 'finishing\\n'; exit 7"))
    adopter = subprocess.Popen(
        [sys.executable, "-c", _ADOPTER, str(checkpoint), str(tmp_path), job["id"]],
        cwd=str(ROOT), env=build_subprocess_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, encoding="utf-8")
    # This test process plays the host serving the owner chat (the hub gateway): a fresh registry
    # that never adopted the job.
    registry = ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setenv("HERMES_SESSION_ID", TUI_KEY)
    try:
        assert json.loads(adopter.stdout.readline()) == {"adopted": 1}
        entry = next(e for e in json.loads(checkpoint.read_text()) if e["session_id"] == job["id"])
        assert entry["writer_pid"] == adopter.pid
        yield job, adopter, gate, registry
    finally:
        gate.touch()
        _reap(registry, job)
        if adopter.poll() is None:
            adopter.kill()
        adopter.wait(10)


def _call(action: str, session_id: str, **args) -> dict:
    return json.loads(_handle_process({"action": action, "session_id": session_id, **args}))


def _adopter_report(adopter) -> dict:
    out, _ = adopter.communicate("\n", timeout=30)
    return json.loads(out.strip().splitlines()[-1])


def test_owner_chat_polls_logs_and_waits_on_a_job_another_process_adopted(adopted_elsewhere, monkeypatch):
    job, adopter, gate, _registry = adopted_elsewhere
    deadline = time.monotonic() + 10
    while "started" not in (polled := _call("poll", job["id"])).get("output_preview", ""):
        assert time.monotonic() < deadline, polled
        threading.Event().wait(0.05)
    assert polled["status"] == "running"
    assert polled["supervised_by_pid"] == adopter.pid
    assert _call("poll", job["id"][:9])["status"] == "running"  # a unique prefix resolves too
    assert "started" in _call("log", job["id"])["output"]
    waited = _call("wait", job["id"], timeout=1)
    assert waited["status"] == "timeout" and waited["process_running"] is True
    # Another chat sees nothing: the fallback serves the owning chat only.
    monkeypatch.setenv("HERMES_SESSION_ID", "20261113_000000_other")
    assert _call("poll", job["id"])["status"] == "not_found"
    monkeypatch.setenv("HERMES_SESSION_ID", TUI_KEY)
    gate.touch()
    waited = _call("wait", job["id"], timeout=20)
    assert waited["status"] == "exited" and waited["exit_code"] == 7, waited
    report = _adopter_report(adopter)
    # The supervisor still sends the one completion notice; the owner chat's calls took nothing over.
    assert report["done"] and report["events"] == [
        {"session_id": job["id"], "exit_code": 7, "completion_reason": "exited"}]


def test_owner_chat_kills_a_job_another_process_adopted(adopted_elsewhere):
    job, adopter, _gate, registry = adopted_elsewhere
    killed = _call("kill", job["id"])
    assert killed["status"] == "killed", killed
    assert registry._detached_host_fate(job["pid"], job["entry"]["host_start_time"]) != "running"
    # Until the supervisor's receipt lands, this chat's own poll reports the kill too.
    polled = _call("poll", job["id"])
    assert (polled["completion_reason"], polled["exit_code"]) == ("killed", -15), polled
    report = _adopter_report(adopter)
    # The supervisor's one notice agrees with the kill result (it said "status unknown").
    assert report["done"] and report["events"] == [
        {"session_id": job["id"], "exit_code": -15, "completion_reason": "killed"}]
    assert report["sources"] == ["process.kill"]
    assert "terminated by process.kill" in report["notices"][0]
    assert "status unknown" not in report["notices"][0]


@pytest.mark.parametrize("failure", ["survivors", "scope_stop"])
def test_foreign_kill_that_leaves_the_job_alive_withdraws_its_kill_marker(adopted_elsewhere, monkeypatch, failure):
    """A kill that did not end the job must not make its later natural exit read as killed."""
    job, adopter, gate, registry = adopted_elsewhere
    if failure == "scope_stop" and not job["entry"].get("systemd_unit"):
        pytest.skip("this host runs the job without a systemd scope")
    monkeypatch.setattr(registry, "_terminate_host_pid", lambda *a, **k: None)
    monkeypatch.setattr("tools.process_registry._stop_systemd_unit", lambda unit: failure != "scope_stop")
    monkeypatch.setattr(registry, "_post_kill_survivors", lambda session: [job["pid"]])
    killed = _call("kill", job["id"])
    assert killed["status"] == "error" and killed["process_running"] is True, killed
    assert not Path(job["log"]).with_suffix(".killed").exists()
    gate.touch()
    report = _adopter_report(adopter)
    assert report["events"] == [{"session_id": job["id"], "exit_code": 7, "completion_reason": "exited"}]


def test_foreign_wait_gets_the_masked_success_hint_like_a_local_one(monkeypatch):
    """The foreign fallback's poll/wait result runs through the same post-processing as a local one."""
    import tools.process_registry_foreign as foreign
    failure = "Traceback (most recent call last)\n  File \"run.py\"\nValueError: boom"
    head = {"status": "exited", "session_id": "proc_feedface0001", "exit_code": 0,
            "command": "python3 run.py 2>&1 | tail -5"}
    for action, key in (("wait", "output"), ("poll", "output_preview")):
        result = {**head, key: failure}
        monkeypatch.setattr(foreign, "foreign_action", lambda registry, a, sid, args, r=result: dict(r))
        answered = _call(action, "proc_feedface0001")
        assert answered["exit_code"] == 0 and "NOT of the command before the pipe" in answered.get("hint", ""), answered


def test_foreign_kill_of_a_job_its_spawner_still_supervises_settles_as_killed(checkpoint, tmp_path, monkeypatch):
    """The supervisor can be the live spawner itself (it holds the Popen): its reader honours the
    kill marker too instead of reporting the signal's bare exit."""
    import tools.process_registry_foreign as foreign
    monkeypatch.setattr("tools.process_registry_output_log.output_dir", lambda: tmp_path)
    registry = ProcessRegistry()
    session = registry.spawn_local("printf 'started\\n'; sleep 30", cwd=str(tmp_path), session_key=TUI_KEY,
                                   owner_task_id=TUI_KEY, persist_on_release=True)
    try:
        assert session.process is not None and session.output_log
        deadline = time.monotonic() + 10
        while "started" not in Path(session.output_log).read_text():
            assert time.monotonic() < deadline, "job never started"
            threading.Event().wait(0.02)
        registry._write_checkpoint()
        entry = next(e for e in json.loads(checkpoint.read_text()) if e["session_id"] == session.id)
        assert foreign.foreign_kill(registry, entry)["status"] == "killed"
        assert session._completion_event.wait(10)
        assert (session.exit_code, session.completion_reason, session.termination_source) == (
            -15, "killed", "process.kill")
    finally:
        if session._reader_thread:
            session._reader_thread.join(5)
        registry.kill_process(session.id)


def test_a_kill_marker_retires_its_log_like_a_recorded_exit(tmp_path, monkeypatch):
    """A killed job records no ``.exit``: its kill marker ages its log out instead (it never would)."""
    from tools import process_registry_output_log as logs
    from tools.process_registry_results import RESULT_RETENTION_SECONDS
    monkeypatch.setattr(logs, "output_dir", lambda: tmp_path)
    old = time.time() - RESULT_RETENTION_SECONDS - 60
    for name in ("proc_killed", "proc_live"):
        (tmp_path / f"{name}.log").write_text("x")
    logs.record_kill(tmp_path / "proc_killed.log", "process.kill")
    os.utime(tmp_path / "proc_killed.killed", (old, old))
    logs.worker_output_log("proc_new", persist=True)
    assert sorted(p.name for p in tmp_path.glob("proc_*")) == ["proc_live.log"]
