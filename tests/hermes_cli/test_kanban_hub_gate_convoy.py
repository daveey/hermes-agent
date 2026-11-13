"""Hub final gate (hub 2026-11-13): hub-local claims were refused as ``hub_cpu_unknown`` with a fresh low sample.

Two things made that undiagnosable and slow. Every final-gate refusal branch (unreadable limit, expired lease, stale
sample, hold overrun) reported the same ``hub_cpu_unknown``, so the skip event could not say which fired. And under
admission.lock the census and the pool/origin lookups read ``PRAGMA table_info`` row by row (one sqlite step per
column: 55 for the hub's ``tasks``, 22 for ``task_runs``; 95 steps for one board's census, 4 now); in the gateway
each step drops the GIL and re-acquires it behind CPU-bound threads, so the lookups cost seconds per claim.
``_Steps`` counts those steps deterministically, with no real contention.
"""
import sqlite3
import types

import pytest

from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_queue_reason as qr
from hermes_cli import machine_metrics as mm
from tests.hermes_cli import hub_admission_support as support
from tests.hermes_cli.hub_admission_support import configure, task

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def board(tmp_path, monkeypatch):
    yield from support.open_board(tmp_path, monkeypatch)


def enable_pools(home):
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")


class _Steps:
    """Counts every sqlite call/row step the census makes."""

    def __init__(self):
        self.n = 0

    def step(self):
        self.n += 1


class _Cursor:
    def __init__(self, cur, steps):
        self._cur, self._steps = cur, steps

    def __iter__(self):
        for row in self._cur:
            self._steps.step()
            yield row

    def fetchone(self):
        self._steps.step()
        return self._cur.fetchone()

    def fetchall(self):
        return list(self)


class _Conn:
    def __init__(self, db, steps):
        self._db, self._steps = db, steps

    def execute(self, sql, params=()):
        self._steps.step()
        return _Cursor(self._db.execute(sql, params), self._steps)

    def close(self):
        self._db.close()

    def __getattr__(self, name):
        return getattr(self._db, name)


def _count_census_steps(monkeypatch, steps):
    real = sqlite3.connect

    def connect(*a, **k):
        steps.step()
        return _Conn(real(*a, **k), steps)
    monkeypatch.setattr(mm, "sqlite3", types.SimpleNamespace(connect=connect, Error=sqlite3.Error,
                                                             OperationalError=sqlite3.OperationalError))


def test_census_sqlite_steps_do_not_scale_with_column_count(board, monkeypatch):
    conn, home = board
    enable_pools(home)
    task(conn)
    steps = _Steps()
    _count_census_steps(monkeypatch, steps)
    detail = {"pools": {"regular": 0, "priority": 0}, "capacity_origins": {}}
    assert mm.active_card_runs(detail=detail) == 0
    assert detail["pools"] == {"regular": 0, "priority": 0}
    assert steps.n <= 12, f"census made {steps.n} sqlite steps for one board"


def test_table_columns_reads_every_column_in_one_row(board):
    conn, _home = board
    names = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    steps = _Steps()
    assert mm.table_columns(_Conn(conn, steps), "tasks") == names and "status" in names
    assert steps.n == 2  # one execute + one row, however many columns
    assert mm.table_columns(conn, "no_such_table") == set()


@pytest.mark.parametrize("module,table", [
    ("hermes_cli.kanban_admission_pools", "task_runs"),
    ("hermes_cli.kanban_admission_origin", "tasks"),
])
def test_lock_held_lookups_read_columns_through_table_columns(module, table):
    """The pool and origin lookups run on the claim connection under admission.lock: they must use the one-row
    ``table_columns``, not iterate ``PRAGMA table_info``."""
    import importlib
    import inspect
    src = inspect.getsource(importlib.import_module(module))
    assert f'table_columns(conn, "{table}")' in src
    assert "PRAGMA table_info" not in src


def test_final_gate_refusals_each_have_their_own_reason():
    reasons = ka.FINAL_GATE_REASONS
    assert len(set(reasons)) == len(reasons) == 5
    assert "hub_cpu_unknown" not in reasons and ka.OVERRUN in reasons


@pytest.mark.parametrize("reason", (*ka.FINAL_GATE_REASONS, ka.HUB_CPU_POOL_LIMIT_UNKNOWN))
def test_queue_line_names_the_branch(reason):
    from hermes_cli import kanban_dispatch_skip as skip
    assert f"({reason})" in skip.words({"reason": "claim_deferred", "detail": reason})
    assert reason in qr.CODES  # a Board queue observation carrying it is kept, not flattened to unavailable


def test_advisory_pool_limit_failure_is_not_reported_as_a_sample_problem(monkeypatch):
    """The advisory's ``hub_cpu_unknown`` means its sample; an unreadable pool limit has its own reason."""
    from hermes_cli import kanban_admission_pools as pools
    from hermes_cli import machine_cpu_metrics as cpu

    def unreadable(*a, **k):
        raise ValueError("priority_slots: wrong")
    monkeypatch.setattr(cpu, "root_limit", lambda: (cpu.ON, 50.0))
    monkeypatch.setattr(pools, "advisory_limit", unreadable)
    monkeypatch.setattr(ka, "sampler_sample", lambda *a, **k: pytest.fail("no sample read after a limit failure"))
    assert ka.hub_cpu_advisory(conn=object()) == ka.HUB_CPU_POOL_LIMIT_UNKNOWN != "hub_cpu_unknown"
    assert ka.HUB_CPU_POOL_LIMIT_UNKNOWN not in ka.FINAL_GATE_REASONS  # advisory-only: the final gate never returns it
