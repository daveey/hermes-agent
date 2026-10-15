"""Live workers survive a gateway restart and a ``kern.boottime`` re-anchor of any size.

macOS reports a process start time as ``kern.boottime`` + the process's uptime offset, so a sleep/wake
or NTP step moves every live worker's start time at once. The spawn fingerprint therefore carries the
boottime read beside the start (``"|<start>@<boottime>"``); liveness compares the boot-relative offset,
which the re-anchor leaves unchanged. A dispatcher in a different process (the restarted gateway, with
a new claimer pid) must keep the live worker running, and that worker must still complete its card.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

REPO = Path(__file__).resolve().parents[2]
FAKE_BOOT = 179_126_347_110  # centiseconds; any stable value works


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def macos_clock(monkeypatch):
    """Model the macOS fingerprint (empty instantiation epoch + boottime stamp) on any platform.

    ``shift(centis)`` re-anchors boottime: every start time AND the boottime read move together.
    """
    from gateway import drain_control, status

    real_start = status._get_process_start_time
    state = {"shift": 0}
    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "")
    monkeypatch.setattr(kbd, "_darwin_boottime_centis", lambda: FAKE_BOOT + state["shift"], raising=False)

    def start(pid):
        value = real_start(pid)
        return None if value is None else value + state["shift"]

    monkeypatch.setattr(status, "_get_process_start_time", start)

    def shift(centis: int) -> None:
        state["shift"] = centis
        kbd._recent_worker_exits.clear()  # a restarted dispatcher has reaped nothing

    return shift


def _running(conn, pid: int, *, claimer: str) -> tuple[str, int]:
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid, claimer=claimer)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    return tid, run_id


def _dead_claimer() -> str:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return f"{kb._host_prefix()}{child.pid}"


def test_fingerprint_carries_boottime_stamp(macos_clock):
    fp = kbd._process_fingerprint(os.getpid())
    assert fp is not None and fp.startswith("|") and fp.endswith(f"@{FAKE_BOOT}")


def test_boottime_reanchor_beyond_tolerance_keeps_live_worker(board, macos_clock):
    """A 30 s boottime step (well past the 2 s absolute tolerance) is not a crash."""
    conn = board
    tids = [_running(conn, os.getpid(), claimer=_dead_claimer())[0] for _ in range(3)]
    stamped = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tids[0],)).fetchone()[0]
    assert "@" in stamped

    macos_clock(3000)
    assert kbd._worker_alive(os.getpid(), stamped) is True
    assert kbd.detect_crashed_workers(conn) == []
    assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", []) == []
    assert kb.release_stale_claims(conn) == 0
    for tid in tids:
        assert kb.get_task(conn, tid).status == "running"


def test_recycled_pid_is_still_foreign_after_reanchor(board, macos_clock):
    """Control: a PID whose boot-relative start is a minute off is a stranger, re-anchor or not."""
    conn = board
    tid, _ = _running(conn, os.getpid(), claimer=_dead_claimer())
    stamped = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    start, boot = kbd._start_components(stamped.split("|", 1)[1])
    foreign = f"|{start - 6000}@{boot}"  # recorded process started 60 s before the live one
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ? WHERE id = ?", (foreign, tid))

    macos_clock(3000)
    assert kbd._worker_alive(os.getpid(), foreign) is False
    assert tid in kbd.detect_crashed_workers(conn)


def test_dead_worker_is_still_crashed_after_reanchor(board, macos_clock):
    conn = board
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    tid, _ = _running(conn, child.pid, claimer=_dead_claimer())
    child.wait()
    macos_clock(3000)
    assert tid in kbd.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).worker_pid is None


def test_restarted_dispatcher_in_another_process_adopts_live_worker(board, tmp_path):
    """Process A spawned ``sleep`` under a now-dead claimer; dispatcher B (a fresh interpreter, new
    claimer pid) sees it alive. The worker then completes its own card with its run id."""
    conn = board
    worker = subprocess.Popen(["sleep", "120"])
    try:
        tid, run_id = _running(conn, worker.pid, claimer=_dead_claimer())
        script = textwrap.dedent(f"""
            import json, sys
            sys.path.insert(0, {str(REPO)!r})
            from pathlib import Path
            from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
            conn = kbc.connect(Path({str(tmp_path / "kanban.db")!r}))
            crashed = kbd.detect_crashed_workers(conn)
            spawned = []
            res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(1) or 1)
            print(json.dumps({{"crashed": crashed, "tick_crashed": res.crashed, "spawned": len(spawned),
                               "status": kb.get_task(conn, {tid!r}).status}}))
        """)
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False,
                             env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        assert out.returncode == 0, out.stderr
        report = json.loads(out.stdout.strip().splitlines()[-1])
        assert report == {"crashed": [], "tick_crashed": [], "spawned": 0, "status": "running"}

        assert kb.complete_task(conn, tid, summary="done", expected_run_id=run_id) is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        worker.kill()
        worker.wait()
