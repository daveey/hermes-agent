"""Hub CPU gate under GIL contention: the whole-host census must not eat the sample's hold (t_01a1d2d60475a488).

Inside the gateway every ``sqlite3`` call releases the GIL and has to win it back from busy Python threads (the Kanban
sync loop, the event loop); under that contention each handoff costs about one switch interval (5 ms). The final gate
ran the whole-host census twice under ``admission.lock`` (cap check before a ~1 s sample, recount after it), inside
a 2.25 s hold; it now runs one census and re-reads the sampler's sample. A census that iterates ``PRAGMA table_info``
and row-by-row cursors makes 95 handoffs per board (measured: the hub's 55-column ``tasks``, 8 running rows; the
one-statement census makes 4), so on the hub (10 boards) it took seconds (7 s measured with one busy thread) and every
hub claim was refused before sampling as ``hub_cpu_unknown`` from 2026-11-13 02:03Z. The model below charges one switch
interval per sqlite call/row step on a virtual clock, so the test is deterministic and fast.
"""
import json
import logging
import sqlite3
import sys
import time
import types
from contextlib import closing

import pytest

from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm
from tests.hermes_cli import hub_admission_support as support
from tests.hermes_cli.hub_admission_support import advisory, claim, fake_sample, refused, task

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def board(tmp_path, monkeypatch):
    yield from support.open_board(tmp_path, monkeypatch)


HUB_BOARDS = 10  # hermes-hub: the default board plus 9 named boards with a kanban.db


class _Clock:
    """``time.monotonic`` plus virtual GIL-handoff and sample time."""

    def __init__(self, handoff_s):
        self.handoff_s, self.offset, self.handoffs = handoff_s, 0.0, 0

    def now(self):
        return time.monotonic() + self.offset

    def tick(self, n=1):
        self.handoffs += n
        self.offset += n * self.handoff_s


class _Cursor:
    def __init__(self, cur, clock):
        self._cur, self._clock = cur, clock

    def __iter__(self):
        for row in self._cur:
            self._clock.tick()
            yield row
        self._clock.tick()  # the final step that returns SQLITE_DONE

    def fetchone(self):
        self._clock.tick()
        return self._cur.fetchone()

    def fetchall(self):
        rows = self._cur.fetchall()
        self._clock.tick(len(rows) + 1)
        return rows


class _Conn:
    def __init__(self, conn, clock):
        self._conn, self._clock = conn, clock

    def execute(self, *a):
        self._clock.tick()
        return _Cursor(self._conn.execute(*a), self._clock)

    def close(self):
        self._clock.tick()
        self._conn.close()


def _contended(monkeypatch, handoff_s):
    """Charge one handoff per census sqlite call on a virtual clock the gate's hold budget reads."""
    clock = _Clock(handoff_s)

    def connect(*a, **k):
        clock.tick()
        return _Conn(sqlite3.connect(*a, **k), clock)
    monkeypatch.setattr(mm, "sqlite3", types.SimpleNamespace(connect=connect, Error=sqlite3.Error,
                                                             OperationalError=sqlite3.OperationalError))
    monkeypatch.setattr(ka, "time", types.SimpleNamespace(monotonic=clock.now, time=time.time))
    return clock


def _hub_boards(conn, home, *, cap=20, synced=False):
    """The hub's shape: pools on (priority slots), the CPU gate on, one running card on each of 10 boards. ``synced``
    boards carry the sync state and lease holders, plus a running row another gateway holds (never counted here)."""
    (home / "config.yaml").write_text(f"kanban:\n  max_in_progress: {cap}\n  admission:\n"
                                      "    cpu_busy_below_pct: 50\n    priority_slots: 3\n")
    advisory(home)
    paths = [home / "kanban.db"]
    for index in range(HUB_BOARDS - 1):
        folder = kb.boards_root() / f"hub-{index}"
        folder.mkdir(parents=True)
        (folder / "board.json").write_text("{}")
        paths.append(folder / "kanban.db")
    for path in paths:
        with closing(kbc.connect(path)) as other:
            busy = kb.create_task(other, title="running elsewhere", assignee="default")
            foreign = kb.create_task(other, title="leased to a peer", assignee="default")
            with kb.write_txn(other):
                other.execute("UPDATE tasks SET status='running', claim_lock='peer' WHERE id=?", (busy,))
                if synced:
                    if "lease_holder" not in {r[1] for r in other.execute("PRAGMA table_info(tasks)")}:
                        other.execute("ALTER TABLE tasks ADD COLUMN lease_holder TEXT")
                    other.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state (gateway_id TEXT NOT NULL)")
                    other.execute("INSERT INTO kanban_sync_state VALUES ('hermes-hub')")
                    other.execute("UPDATE tasks SET lease_holder='hermes-hub' WHERE id=?", (busy,))
                    other.execute("UPDATE tasks SET status='running', lease_holder='worker-9' WHERE id=?", (foreign,))


@pytest.mark.parametrize("synced", [True, False])
@pytest.mark.parametrize("review", [False, True])
def test_contended_census_fits_inside_the_hold(board, monkeypatch, review, synced):
    """RED before the census fix: 95 handoffs per board x 10 boards overran the hold on every claim. The gate now
    runs one census (4 steps per board) and re-reads the sampler's sample (never samples), so the claim commits
    inside 2.25 s."""
    conn, home = board
    _hub_boards(conn, home, synced=synced)
    t = task(conn, review)
    # Setup opens ten real boards; it is not part of the measured census hold.
    # Seed the sampler after setup so slow storage cannot age it past the 10 s prelease bound.
    advisory(home)
    clock = _contended(monkeypatch, sys.getswitchinterval())
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("the locked gate reads the sampler, never samples"))
    claimed = claim(conn, t, review)
    assert claimed is not None, (sync.pop_claim_deferral(t), clock.handoffs)
    # One census over 10 boards: a handful of sqlite calls per board (a second census would double it).
    assert clock.handoffs <= HUB_BOARDS * 8, clock.handoffs
    payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='claimed' "
                                      "ORDER BY id DESC", (t,)).fetchone()[0])
    assert payload["hub_admission"]["pools"] == {"regular": HUB_BOARDS, "priority": 0}


def test_census_refreshes_sample_after_slow_setup(board, monkeypatch):
    """Ten-board setup may outlive prelease freshness before any census handoff occurs."""
    real_task = task

    def aged_setup(*args, **kwargs):
        result = real_task(*args, **kwargs)
        advisory(board[1], age_ms=ka.PRELEASE_MAX_AGE_MS + 1)
        return result

    monkeypatch.setattr(sys.modules[__name__], "task", aged_setup)
    test_contended_census_fits_inside_the_hold(board, monkeypatch, review=False, synced=False)


@pytest.mark.parametrize("synced", [True, False])
def test_one_read_census_matches_the_long_way(board, monkeypatch, synced):
    """Control: counts, pool receipts, capacity origins, the own-holder filter and owners read exactly as the
    statement-per-column path (still used for boards whose schema predates receipts)."""
    conn, home = board
    _hub_boards(conn, home, synced=synced)
    with kb.write_txn(conn):  # a capacity receipt with an origin, on the priority pool
        marked = kb.create_task(conn, title="marked", assignee="default")
        conn.execute("UPDATE tasks SET status='running', claim_lock='x' WHERE id=?", (marked,))
        run = conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, admission_pool, "
                           "admission_class, admission_origin) VALUES (?, 'default', 'running', 1, 'priority', "
                           "'capacity', 'origin-a')", (marked,)).lastrowid
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run, marked))

    def census():
        detail, owners = {"pools": {"regular": 0, "priority": 0}, "capacity_origins": {}}, set()
        return mm.active_card_runs(detail=detail, owner_output=owners), detail, owners, mm.active_card_runs()
    fast = census()
    assert fast[0] == HUB_BOARDS + 1 and fast[3] == HUB_BOARDS + 1
    assert fast[1] == {"pools": {"regular": HUB_BOARDS, "priority": 1}, "capacity_origins": {"origin-a": 1},
                       "excluded": 0}
    monkeypatch.setattr(mm, "_running_rows", lambda db: None)
    assert census() == fast


def test_an_overrun_names_its_stage_and_hold_in_the_refusal(board, monkeypatch, caplog):
    """A hold that still cannot fit (heavier contention) refuses as ``hub_admission_overrun`` (never as a CPU
    reading) with the stage and the measured hold logged, and never measures CPU under the lock."""
    conn, home = board
    _hub_boards(conn, home)
    t = task(conn)
    clock = _contended(monkeypatch, 0.2)  # 200 ms per handoff: even a few calls per board overrun 2.25 s
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("the locked gate reads the sampler, never samples"))
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_admission"):
        assert kb.claim_task(conn, t) is None
    refused(conn, t, False, ka.OVERRUN)
    line = next(r.getMessage() for r in caplog.records if "Hub admission refused" in r.getMessage())
    assert "'stage': 'hold_after_census'" in line, line
    assert "'hold_ms':" in line and "'budget_ms': 2250" in line, line
    assert clock.handoffs > 0


def _expires_during_census(mp):
    calls = iter([False, True])  # live at the first check, past due at the one after the census
    mp.setattr(ka, "_expired", lambda gate: next(calls, True))


def _limit_unreadable_at_the_lock(mp):
    # The pre-lease advisory would refuse first with the same code; skip it so only the locked gate reads the limit.
    mp.setattr(kb, "hub_cpu_advisory", lambda task_id=None, *, conn=None: None)
    mp.setattr(cm, "root_limit", lambda *a, **k: (cm.UNKNOWN, None))


@pytest.mark.parametrize("reason,stage,setup", [
    (ka.HUB_CPU_LIMIT_UNKNOWN, "limit_unknown", _limit_unreadable_at_the_lock),
    # The pre-lease advisory passes a max age; only the locked gate's re-read (no argument) finds the sample gone.
    (ka.HUB_CPU_SAMPLE_STALE, "sample_stale_or_missing", lambda mp: mp.setattr(
        ka, "sampler_sample", lambda max_age_ms=None, _real=ka.sampler_sample:
        _real(max_age_ms) if max_age_ms is not None else None)),
    (ka.HUB_CPU_GATE_EXPIRED, "lease_expired_before_census", lambda mp: mp.setattr(ka, "_expired", lambda gate: True)),
    (ka.HUB_CPU_GATE_EXPIRED_COUNTING, "lease_expired_after_census", _expires_during_census),
])
def test_each_refusal_branch_names_its_reason_and_stage(board, monkeypatch, caplog, reason, stage, setup):
    conn, home = board
    (home / "config.yaml").write_text("kanban:\n  admission:\n    cpu_busy_below_pct: 50\n")
    advisory(home)
    monkeypatch.setattr(sync, "claim_gate", lambda conn, task_id, expires, lane="ready": sync.ClaimGate(expires))
    setup(monkeypatch)
    t = task(conn)
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_admission"):
        assert kb.claim_task(conn, t) is None
    refused(conn, t, False, reason)
    line = next(r.getMessage() for r in caplog.records if "Hub admission refused" in r.getMessage())
    assert f"'stage': '{stage}'" in line, line
