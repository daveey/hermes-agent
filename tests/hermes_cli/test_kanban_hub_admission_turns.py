"""Turn sources vs the hub claim's ~1 s sample under admission.lock (review F1).

The frozen contract keeps the independent full CPU sample entirely under admission.lock. A chat, notification or
review turn that reserves admission during that hold must be admitted once the hold ends, never refused with 5035:
turn sources wait longer than the longest designed hold, and a waiting turn makes the next claims defer (claims
retry on the next tick; a typed message has nowhere else to go).
"""
import threading
import time

import pytest

from hermes_cli import admission_fence as af
from hermes_cli import backend_retirement
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm
from tests.waiting import wait_until
from tests.hermes_cli.hub_admission_support import locked

pytestmark = pytest.mark.platforms("posix")

HOLD_S = 1.2  # the measured hub-claim hold (lock-measure.jsonl: 1.20-1.26 s)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("kanban:\n  admission:\n    cpu_busy_below_pct: 50\n")
    mm.record_hub_cpu_sample({"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000,
                              "cpu_busy_sample_end_ms": int(time.time() * 1000)}, root=tmp_path)
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn, tmp_path
    finally:
        conn.close()


def ready(conn, title="gate"):
    t = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (t,))
    return t


def slow_sample(started, hold_s=HOLD_S, ended=None):
    def sample(*a, **k):
        started.set()
        time.sleep(hold_s)  # health: allow HX013 -- stands in for the real ~1 s sample (+ commit) held under the lock
        if ended is not None:
            ended.append(time.monotonic())
        return {"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": int(time.time() * 1000)}
    return sample


def test_turn_wait_budget_outlasts_the_longest_designed_hold():
    # Frozen bound on one hub claim's hold (test_kanban_hub_admission: claim_s <= 2.5) is the shared constant.
    assert af.CLAIM_MAX_HOLD_S == 2.5
    assert backend_retirement.BUSY_WAIT_S > af.CLAIM_MAX_HOLD_S


def test_turn_reserving_during_a_hub_claim_sample_is_admitted_after_the_hold(board, monkeypatch):
    """The real turn-source path (retirement.reservation, as typed submit / notifications use it) started inside the
    claim's held sample is admitted once the claim commits: no 5035, and the claim still sampled under the lock."""
    conn, home = board
    started, ended = threading.Event(), []
    locked(monkeypatch, slow_sample(started, ended=ended))
    t = ready(conn)
    m = {}

    def turn():
        started.wait(5)
        m["try"] = time.monotonic()
        with backend_retirement.retirement.reservation(home) as admitted:
            m["admitted"] = admitted
            m["refusal"] = backend_retirement.last_refusal()
            m["at"] = time.monotonic()
    th = threading.Thread(target=turn)
    th.start()
    got = kb.claim_task(conn, t)
    th.join(10)
    assert got is not None
    assert m["admitted"] is True and m["refusal"] is None, m
    assert m["at"] >= ended[0]  # admitted only after the claim's sample (held under the lock) finished
    assert m["at"] - m["try"] <= backend_retirement.BUSY_WAIT_S + 0.1


def test_a_waiting_turn_makes_the_next_claim_defer_without_sampling(board, monkeypatch):
    conn, home = board
    locked(monkeypatch, lambda *a, **k: pytest.fail("a waiting turn: defer before sampling"))
    t = ready(conn)
    with af.turn_waiting(home):
        assert af.turns_waiting(home / "core-update")
        assert kb.claim_task(conn, t) is None
        assert kb.get_task(conn, t).status == "ready"
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
        # Publish the actual waiting-turn cause without changing admission or opening a run.
        assert sync.pop_claim_deferral(t) == 'turn_admission_waiting'
        assert sync.pop_claim_deferral(t) is None  # one-shot diagnostic, not a retained hold
    assert not af.turns_waiting(home / "core-update")
    locked(monkeypatch, slow_sample(threading.Event(), hold_s=0))
    assert kb.claim_task(conn, t) is not None


def test_busy_reservation_registers_as_a_waiting_turn_until_admitted(board):
    _conn, home = board
    m = {}
    with af.admission_lock(home, timeout=1):
        th = threading.Thread(target=lambda: m.setdefault("r", backend_retirement._reserve(
            home, backend_retirement.BUSY_WAIT_S, "test turn")))
        th.start()
        wait_until(lambda: af.turns_waiting(home / "core-update"), timeout=5, what="turn registered as waiting")
    th.join(10)
    pending, reason = m["r"]
    try:
        assert pending is not None and reason is None
    finally:
        pending.close()
    assert not af.turns_waiting(home / "core-update")


def test_crashed_waiter_marker_is_not_a_waiting_turn(board):
    _conn, home = board
    directory = home / "core-update" / af.TURN_WAITERS
    directory.mkdir(parents=True)
    (directory / "dead-owner").write_text("")
    assert not af.turns_waiting(home / "core-update")
    assert not (directory / "dead-owner").exists()


def test_update_hold_still_refuses_a_waiting_turn_at_once(board):
    _conn, home = board
    af.set_hold(home)
    try:
        t0 = time.monotonic()
        pending, reason = backend_retirement._reserve(home, backend_retirement.BUSY_WAIT_S, "test turn")
        assert pending is None and reason == "held"
        assert time.monotonic() - t0 < 0.5
    finally:
        af.clear_hold(home)
