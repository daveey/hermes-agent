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


def _timed_lock_child(home, ready, hold_s):
    import time
    with af.admission_lock(home):
        ready.set()
        time.sleep(hold_s)  # health: allow HX013 -- how long another process holds the lock IS the contention under test


def _spawn_lock_holder(home, hold_s):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_timed_lock_child, args=(home, ready, hold_s))
    child.start()
    assert ready.wait(20)
    return child


def _reap(child):
    if child.is_alive():
        child.kill()
    child.join(10)


def _busy_records(caplog):
    return [r for r in caplog.records if r.name == "hermes_cli.backend_retirement" and r.levelno >= 30]


def test_reservation_survives_lock_briefly_busy_without_hold(tmp_path, caplog):
    """[daveey] Another process holding admission.lock for 200 ms (no update hold) must not refuse a
    turn: hermes-sync's state read and Kanban claims hold it that long on a loaded Mac (agent-6f2f71c662)."""
    import logging
    caplog.set_level(logging.DEBUG, logger="hermes_cli.backend_retirement")
    child = _spawn_lock_holder(tmp_path, 0.2)
    try:
        fence = RetirementFence()
        with fence.reservation(tmp_path) as admitted:
            assert admitted
            assert not child.is_alive() or af.state(tmp_path)["reservations"] == 1
        assert not _busy_records(caplog)
    finally:
        _reap(child)
    assert af.state(tmp_path)["reservations"] == 0


def test_reservation_refuses_long_busy_lock_with_one_warning(tmp_path, caplog):
    import logging
    import time
    caplog.set_level(logging.DEBUG, logger="hermes_cli.backend_retirement")
    child = _spawn_lock_holder(tmp_path, 2.0)
    try:
        fence = RetirementFence()
        started = time.monotonic()
        with fence.reservation(tmp_path) as admitted:
            elapsed = time.monotonic() - started
            assert not admitted
        assert 0.45 <= elapsed < 1.5
        records = _busy_records(caplog)
        assert len(records) == 1, [r.getMessage() for r in records]
        message = records[0].getMessage()
        assert "update admission lock busy for" in message and "no update hold" in message
        assert records[0].exc_info is None
        from hermes_cli.backend_retirement import last_refusal
        assert last_refusal() == "busy"
        assert fence.active_count() == 0
    finally:
        _reap(child)


def test_hold_refuses_at_once_without_busy_retry(tmp_path, caplog):
    import logging
    import time
    caplog.set_level(logging.DEBUG, logger="hermes_cli.backend_retirement")
    af.set_hold(tmp_path)
    fence = RetirementFence()
    started = time.monotonic()
    with fence.reservation(tmp_path, busy_wait_s=5.0) as admitted:
        assert not admitted
    assert time.monotonic() - started < 0.3
    assert not _busy_records(caplog)
    from hermes_cli.backend_retirement import last_refusal
    assert last_refusal() == "held"


def test_zero_busy_wait_makes_one_short_try(tmp_path):
    """The prompt-admission path holds the session resume lock: it keeps a single 50 ms try."""
    import time
    child = _spawn_lock_holder(tmp_path, 2.0)
    try:
        fence = RetirementFence()
        started = time.monotonic()
        with fence.reservation(tmp_path, busy_wait_s=0) as admitted:
            assert not admitted
        assert time.monotonic() - started < 0.3
        session = {"history_lock": threading.RLock(), "profile_home": str(tmp_path)}
        from hermes_cli import backend_retirement
        real = backend_retirement.retirement
        backend_retirement.retirement = fence
        try:
            started = time.monotonic()
            with server._session_turn_admission(session, busy_wait_s=0) as admitted:
                assert not admitted
            assert time.monotonic() - started < 0.3
        finally:
            backend_retirement.retirement = real
        from hermes_cli.backend_retirement import single_lock_try
        started = time.monotonic()
        with single_lock_try():
            with fence.reservation(tmp_path) as admitted:
                assert not admitted
        assert time.monotonic() - started < 0.3
    finally:
        _reap(child)


def test_busy_refusal_text_says_briefly_busy(tmp_path, monkeypatch):
    """prompt.submit's 5035 names a busy lock as briefly busy, not as a paused update."""
    from hermes_cli import backend_retirement
    fence = RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = {"history_lock": threading.RLock(), "profile_home": str(tmp_path), "session_key": "k"}
    child = _spawn_lock_holder(tmp_path, 2.0)
    try:
        error, _ = server._lock_in_submit_turn("r", "s", session, "hi", {}, False, None, None, None)
    finally:
        _reap(child)
    assert error["error"]["code"] == 5035
    assert "briefly busy" in error["error"]["message"]
    af.set_hold(tmp_path)
    error, _ = server._lock_in_submit_turn("r", "s", session, "hi", {}, False, None, None, None)
    assert "update admission is paused" in error["error"]["message"]


def test_background_review_survives_briefly_busy_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    child = _spawn_lock_holder(tmp_path, 0.2)
    try:
        fence = RetirementFence()
        hold = fence.begin_background_review()
        assert hold is not None
        fence.end_background_review(hold)
    finally:
        _reap(child)


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
