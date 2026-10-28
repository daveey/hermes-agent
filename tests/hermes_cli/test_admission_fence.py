"""Real flock tests for the updater's cross-process admission protocol."""
import json
import multiprocessing

from pathlib import Path
import subprocess
import sys
import threading

import pytest

from hermes_cli import admission_fence as af
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.backend_retirement import RetirementFence
from tui_gateway import server  # bootstrap before the real-home guard

pytestmark = pytest.mark.platforms("posix")


def _reservation_child(home, ready, finish):
    reservation = af.reserve(home)
    ready.set()
    finish.wait(20)
    reservation.close()


def _lock_child(home, ready, finish):
    with af.admission_lock(home):
        ready.set()
        finish.wait(20)


def test_hold_refuses_and_existing_reservation_survives(tmp_path):
    reservation = af.reserve(tmp_path)
    assert af.state(tmp_path)["reservations"] == 1
    assert af.set_hold(tmp_path) == {"version": 1, "held": True, "reservations": 1}
    assert af.reserve(tmp_path) is None
    reservation.close()
    reservation.close()
    assert af.state(tmp_path)["reservations"] == 0
    af.clear_hold(tmp_path)
    af.reserve(tmp_path).close()


def test_cross_process_reservation_and_crash_cleanup(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, finish = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_reservation_child, args=(tmp_path, ready, finish))
    child.start()
    try:
        assert ready.wait(10)
        assert af.set_hold(tmp_path)["reservations"] == 1
        child.kill()
        child.join(10)
        assert af.state(tmp_path) == {"version": 1, "held": True, "reservations": 0}
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)


def test_lock_timeout_and_process_crash_releases_lock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, finish = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_lock_child, args=(tmp_path, ready, finish))
    child.start()
    try:
        assert ready.wait(10)
        with pytest.raises(TimeoutError):
            with af.admission_lock(tmp_path, timeout=0.05):
                pytest.fail("crossed process lock")
        child.kill()
        child.join(10)
        assert af.state(tmp_path)["reservations"] == 0
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)


def test_retirement_composition_and_profile_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile = tmp_path / "profiles" / "test"
    profile.mkdir(parents=True)
    fence = RetirementFence()
    with fence.reservation(profile) as admitted:
        assert admitted
        assert af.state(tmp_path)["reservations"] == 1
        assert fence.prepare() == {"ok": False, "idle": False}
        af.set_hold(tmp_path)
    assert fence.active_count() == 0
    with fence.reservation(profile) as admitted:
        assert not admitted
    assert fence.active_count() == 0
    assert af.state(tmp_path)["reservations"] == 0


def test_desktop_pending_and_worker_are_counted(tmp_path, monkeypatch):
    from hermes_cli import backend_retirement
    fence = RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = {"history_lock": threading.RLock(), "profile_home": str(tmp_path)}
    with server._session_turn_admission(session) as admitted:
        assert admitted
        assert af.state(tmp_path)["reservations"] == 1
    entered, finish = threading.Event(), threading.Event()

    def work():
        entered.set()
        assert finish.wait(10)

    thread = server._start_session_work(work, name="test-update-work", session=session)
    try:
        assert entered.wait(10)
        assert af.set_hold(tmp_path)["reservations"] == 1
        with server._session_turn_admission(session) as admitted:
            assert not admitted
        # An already admitted turn may hand off more stages under the hold;
        # an independent new session must still be denied.
        fresh = {"history_lock": threading.RLock(), "profile_home": str(tmp_path)}
        assert server._start_session_work(lambda: pytest.fail("ran"), name="denied", session=fresh) is None
    finally:
        finish.set()
        thread.join(10)
    assert fence.active_count() == 0
    assert af.state(tmp_path)["reservations"] == 0


@pytest.mark.parametrize("review", [False, True])
def test_hold_blocks_kanban_claims_without_mutation(tmp_path, monkeypatch, review):
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task = kb.create_task(conn, title="held", assignee="default")
        status = "review" if review else "ready"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
        claim = kb.claim_review_task if review else kb.claim_task
        af.set_hold(tmp_path)
        assert claim(conn, task) is None
        assert kb.get_task(conn, task).status == status
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
        af.clear_hold(tmp_path)
        assert claim(conn, task).status == "running"
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.parametrize("review", [False, True])
def test_hold_skips_board_service_lease_before_claiming(tmp_path, monkeypatch, review):
    """[daveey] A held fence must refuse BEFORE the Board-service lease: acquiring one and releasing it on
    refusal records a claimed+released run on the service per ready card per tick (1129 junk runs over one
    11-minute drain window on 2026-10-28)."""
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    gates = []
    real_gate = kb._sync.claim_gate

    def counting_gate(*args, **kwargs):
        gates.append(args[1] if len(args) > 1 else kwargs.get("task_id"))
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(kb._sync, "claim_gate", counting_gate)
    try:
        task = kb.create_task(conn, title="held", assignee="default")
        status = "review" if review else "ready"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
        claim = kb.claim_review_task if review else kb.claim_task
        af.set_hold(tmp_path)
        for _ in range(3):
            assert claim(conn, task) is None
        assert gates == []
        af.clear_hold(tmp_path)
        assert claim(conn, task).status == "running"
        assert gates == [task]
    finally:
        conn.close()


def test_claim_rejects_nested_transaction_before_waiting(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        with kb.write_txn(conn), af.admission_lock(tmp_path):
            with pytest.raises(RuntimeError, match="existing transaction"):
                kb.claim_task(conn, "not-needed")
    finally:
        conn.close()


@pytest.mark.parametrize("review", [False, True])
def test_claim_fence_spans_commit_and_hooks_run_after_release(tmp_path, monkeypatch, review):
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    observed = []
    try:
        task = kb.create_task(conn, title="publication", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", ("review" if review else "ready", task))

        def trace(sql):
            if sql == "COMMIT":
                try:
                    with af.admission_lock(tmp_path, timeout=0):
                        observed.append("unfenced")
                except TimeoutError:
                    observed.append("fenced")

        def hook(*args):
            with af.admission_lock(tmp_path, timeout=0):
                observed.append("hook-unlocked")

        monkeypatch.setattr(kb, "_fire_task_hook", hook)
        conn.set_trace_callback(trace)
        assert (kb.claim_review_task if review else kb.claim_task)(conn, task)
        conn.set_trace_callback(None)
        assert observed == (["fenced"] if review else ["fenced", "hook-unlocked"])
        assert af.set_hold(tmp_path)["held"]
        with kbc.connect(tmp_path / "kanban.db") as reader:
            assert kb.get_task(reader, task).status == "running"
            assert reader.execute("SELECT count(*) FROM task_runs WHERE status='running'").fetchone()[0] == 1
    finally:
        conn.close()


def test_retirement_idle_probe_can_read_fence_without_deadlock(tmp_path, monkeypatch):
    from hermes_cli import web_server_idle_proof
    fence = RetirementFence()
    monkeypatch.setattr(web_server_idle_proof, "idle_proof", lambda: {"idle": af.state(tmp_path)["reservations"] == 0})
    token = fence.prepare()["token"]
    with fence.reservation(tmp_path) as admitted:
        assert not admitted
    assert fence.cancel(token)["ok"]
    with fence.reservation(tmp_path) as admitted:
        assert admitted


def test_standalone_capability_uses_only_stdlib():
    result = subprocess.run([sys.executable, "-S", str(Path(af.__file__)), "capability"],
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == af.CAPABILITY
