"""Nested ``sync.detail/<step>.<part>`` marks (diagnostic only, ``outer=False``). [daveey]

``kanban_sync_cpu.mark`` accumulates thread CPU per (step, part) in a thread-local table with no lock and
``flush_marks`` publishes it at lock-free points. These tests pin identity (same results, exceptions and
SQL), exact aggregation, publication points, per-statement attribution and lock order at the real sites.
"""
import re
import sqlite3
import threading

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_board_watch as watch
from hermes_cli import kanban_sync_cpu as cpu
from hermes_cli import kanban_sync_outbox_age as age
from hermes_cli import loop_accounting
from hermes_cli import session_throughput_revision as rev


@pytest.fixture
def measured(monkeypatch):
    clock = [0.0]
    records = []
    monkeypatch.setattr(cpu.time, "thread_time", lambda: clock[0])
    monkeypatch.setattr(cpu.loop_accounting, "record", lambda name, **kw: records.append((name, kw)))
    monkeypatch.setattr(cpu, "_local", threading.local())
    monkeypatch.setattr(cpu.loop_accounting, "scopes_enabled", lambda: False)
    return clock, records


def marks(records):
    return {name: kw for name, kw in records if name.startswith("sync.detail/")}


def test_outside_a_step_mark_is_a_plain_call(measured):
    _clock, records = measured
    assert cpu.mark("full.query", lambda a, b=2: (a, b), 1, b=3) == (1, 3)
    assert not getattr(cpu._local, "pending", None)
    cpu.flush_marks()
    assert records == []


def test_other_thread_is_not_attributed(measured):
    _clock, records = measured

    def body():
        out = []
        t = threading.Thread(target=lambda: out.append(cpu.mark("full.query", lambda: 7)))
        t.start()
        t.join(5)
        assert out == [7]
        assert not hasattr(cpu._local, "pending")

    def after_round():
        body()
    cpu.step(after_round)
    assert marks(records) == {}


def test_unknown_part_is_refused_inside_a_step(measured):
    def body():
        with pytest.raises(ValueError):
            cpu.mark("not.a.part", lambda: None)

    def named():
        body()
    cpu.step(named)


def test_exact_count_and_sum_published_once_at_step_exit(measured):
    clock, records = measured

    def after_round():
        for cost in (1.0, 2.0, 4.0):
            cpu.mark("full.query", lambda c=cost: clock.__setitem__(0, clock[0] + c))
        assert marks(records) == {}  # nothing published inside the step body
    cpu.step(after_round)
    got = marks(records)
    assert set(got) == {"sync.detail/after_round.full.query"}
    kw = got["sync.detail/after_round.full.query"]
    assert kw["rows"] == 3 and kw["cpu_s"] == 7.0 and kw["outer"] is False
    assert not cpu._local.pending


def test_identity_value_exception_and_flush_on_exception(measured):
    clock, records = measured
    boom = ValueError("x", 1)

    def raising():
        raise boom

    def select():
        cpu.mark("full.query", lambda: clock.__setitem__(0, clock[0] + 3))
        return cpu.mark("full.query", raising)

    with pytest.raises(ValueError) as caught:
        cpu.step(select)
    assert caught.value is boom and caught.value.args == ("x", 1)
    kw = marks(records)["sync.detail/select.full.query"]
    assert kw["rows"] == 2 and kw["cpu_s"] == 3.0  # raising call booked too


def test_exception_in_detail_flushes_marks_and_propagates_the_original(measured):
    clock, records = measured
    boom = KeyError("k")

    def body():
        cpu.mark("stamp.row", lambda: clock.__setitem__(0, clock[0] + 2))
        raise boom

    def report():
        try:
            return cpu.detail("collect", body)
        finally:
            # the failing detail already published its marks, before the step ends
            assert "sync.detail/report.stamp.row" in marks(records)
    with pytest.raises(KeyError) as caught:
        cpu.step(report)
    assert caught.value is boom
    got = marks(records)
    assert got["sync.detail/report.stamp.row"]["rows"] == 1
    assert "sync.detail/report.collect" in got
    names = [n for n, _ in records if n.startswith("sync.detail/")]
    assert names.index("sync.detail/report.collect") < names.index("sync.detail/report.stamp.row")


def test_detail_flush_runs_after_its_own_record_and_with_exact_rows(measured):
    clock, records = measured

    def step_fn():
        cpu.detail("census", lambda: [cpu.mark("schema.rows", lambda: clock.__setitem__(0, clock[0] + 5))
                                      for _ in range(2)])
        # Published by the detail's own exit, not held for the step's end.
        assert [n for n, _ in records] == ["sync.detail/step_fn.census", "sync.detail/step_fn.schema.rows"]
        assert not cpu._local.pending
    cpu.step(step_fn)
    names = [n for n, _ in records]
    assert names == ["sync.detail/step_fn.census", "sync.detail/step_fn.schema.rows"]
    assert records[1][1]["rows"] == 2 and records[1][1]["cpu_s"] == 10.0


def test_bare_step_publishes_without_any_detail(measured):
    _clock, records = measured

    def acquire():
        cpu.mark("acq.recover", lambda: None)
    cpu.step(acquire)
    assert marks(records)["sync.detail/acquire.acq.recover"]["rows"] == 1


def test_nested_step_keys_by_the_step_active_at_mark_time(measured):
    _clock, records = measured

    def inner():
        cpu.mark("full.close", lambda: None)

    def outer():
        cpu.mark("full.acquire", lambda: None)
        cpu.step(inner)
        assert "sync.detail/inner.full.close" in marks(records)  # the inner step published at its exit
        cpu.mark("full.query", lambda: None)
    cpu.step(outer)
    assert set(marks(records)) == {
        "sync.detail/outer.full.acquire", "sync.detail/outer.full.query", "sync.detail/inner.full.close"}


def test_parent_covers_nested_children(measured):
    clock, records = measured

    def advance(c):
        clock[0] += c

    def full_query():
        advance(1)
        cpu.mark("stamp.row", advance, 2)
        advance(4)

    def sample():
        cpu.detail("sample_full", lambda: cpu.mark("full.query", full_query))
    cpu.step(sample)
    got = marks(records)
    assert got["sync.detail/sample.full.query"]["cpu_s"] == 7.0
    assert got["sync.detail/sample.stamp.row"]["cpu_s"] == 2.0
    assert got["sync.detail/sample.sample_full"]["cpu_s"] >= got["sync.detail/sample.full.query"]["cpu_s"]
    assert got["sync.detail/sample.full.query"]["cpu_s"] >= got["sync.detail/sample.stamp.row"]["cpu_s"]


def test_reader_open_and_keep_flush(measured):
    _clock, records = measured
    cpu._local.step = "wait"
    cpu.mark("schema.cookie", lambda: None)
    cpu.note_reader_open(0.5, "changed")
    assert "sync.detail/wait.schema.cookie" in marks(records)
    cpu.mark("schema.rows", lambda: None)
    cpu.note_reader_keep(0.25)
    assert "sync.detail/wait.schema.rows" in marks(records)
    assert not cpu._local.pending


def test_marked_cm_times_both_halves_and_keeps_exception_semantics(measured):
    clock, records = measured
    seen = []

    class CM:
        def __enter__(self):
            clock[0] += 2
            seen.append("in")
            return "conn"

        def __exit__(self, *exc):
            clock[0] += 3
            seen.append(("out", exc[0]))
            return False

    def step_fn():
        with cpu.marked_cm(CM(), "full.acquire", "full.close") as conn:
            assert conn == "conn"
        with pytest.raises(RuntimeError):
            with cpu.marked_cm(CM(), "full.acquire", "full.close"):
                raise RuntimeError("body")
    cpu.step(step_fn)
    assert seen == ["in", ("out", None), "in", ("out", RuntimeError)]
    got = marks(records)
    assert got["sync.detail/step_fn.full.acquire"]["cpu_s"] == 4.0
    assert got["sync.detail/step_fn.full.close"]["cpu_s"] == 6.0


def test_marked_cm_honours_a_suppressing_exit_and_a_failed_enter(measured):
    class Quiet:
        def __enter__(self):
            return 1

        def __exit__(self, *exc):
            return True

    with cpu.marked_cm(Quiet(), "full.acquire", "full.close"):
        raise RuntimeError("swallowed")

    class Bad:
        exited = False

        def __enter__(self):
            raise OSError("no")

        def __exit__(self, *exc):
            Bad.exited = True

    with pytest.raises(OSError):
        with cpu.marked_cm(Bad(), "full.acquire", "full.close"):
            pass
    assert Bad.exited is False


# ---------------------------------------------------------------- real sites

class Proxy:
    """A connection whose statements cost a distinct fake CPU each."""

    def __init__(self, clock, costs, results):
        self.clock, self.costs, self.results = clock, costs, results

    def execute(self, sql, *args):
        for needle, cost in self.costs.items():
            if needle in sql:
                self.clock[0] += cost
        got = self.results[next(k for k in self.results if k in sql)]
        return type("Cur", (), {"fetchall": lambda s: got, "fetchone": lambda s: got[0]})()


def test_census_parts_are_attributed_per_statement(measured, monkeypatch):
    clock, records = measured
    monkeypatch.setattr(watch.os, "pread", lambda *a: b"", raising=False)

    def header(real, off, n):
        clock[0] += 1
        return b"\x00\x00\x00\x04"
    monkeypatch.setattr(watch, "pread_lock_safe", header)
    conn = Proxy(clock, {"schema_version": 10, "sqlite_schema": 100}, {"schema_version": [(5,)], "sqlite_schema": [("t",)]})

    def census():
        return watch.parsed_from(conn, "/x")
    out = cpu.step(census)
    assert out == [b"\x00\x00\x00\x04", (5,), ("t",)]
    got = marks(records)
    assert got["sync.detail/census.schema.header"]["cpu_s"] == 1
    assert got["sync.detail/census.schema.cookie"]["cpu_s"] == 10
    assert got["sync.detail/census.schema.rows"]["cpu_s"] == 100


def test_census_equals_the_original_pair_on_a_real_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE a (x)")
    expected = (conn.execute("PRAGMA schema_version").fetchall()
                + conn.execute("SELECT type, name, tbl_name, rootpage, sql FROM sqlite_schema ORDER BY rowid").fetchall())
    assert watch.schema_rows(conn) == expected


def _outbox_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kanban_sync_outbox (op_id, state, retry_after, created_at)")
    conn.execute("INSERT INTO kanban_sync_outbox VALUES ('op', 'pending', NULL, 1000)")
    return conn


def test_sample_full_parts_close_on_normal_and_failure_exits(measured, monkeypatch):
    clock, records = measured
    import contextlib
    opened = []

    @contextlib.contextmanager
    def connect_closing(path, **kw):
        clock[0] += 2
        conn = _outbox_conn()
        opened.append(conn)
        try:
            yield conn
        finally:
            clock[0] += 3
            conn.close()
    monkeypatch.setattr(kbc, "connect_closing", connect_closing)
    from hermes_cli import kanban_sync_backlog as backlog
    monkeypatch.setattr(backlog, "behind", lambda p: clock.__setitem__(0, clock[0] + 5) or False)
    monkeypatch.setattr(age, "_since", lambda created: float(created))

    assert cpu.step(lambda: age._sample_full("p")) == ("op", 1000.0)
    got = marks(records)
    assert got["sync.detail/<lambda>.full.acquire"]["cpu_s"] == 2
    assert got["sync.detail/<lambda>.full.query"]["cpu_s"] == 5
    assert got["sync.detail/<lambda>.full.close"]["cpu_s"] == 3
    with pytest.raises(sqlite3.Error):
        opened[0].execute("SELECT 1")  # closed after the normal exit

    monkeypatch.setattr(backlog, "behind", lambda p: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        cpu.step(lambda: age._sample_full("p"))
    with pytest.raises(sqlite3.Error):
        opened[1].execute("SELECT 1")  # closed after the failing exit too


def test_read_stamp_parts_and_original_decisions(measured, monkeypatch):
    clock, records = measured

    def stub(name, cost, value):
        def fn(conn):
            clock[0] += cost
            return value
        monkeypatch.setattr(rev, name, fn)
    stub("_supported_shape", 1, True)
    stub("_tables_exact", 10, True)
    stub("_triggers_exact", 100, True)
    stub("_invariant_ok", 1000, True)
    monkeypatch.setattr(rev, "anchor_matches", lambda conn, r, n: clock.__setitem__(0, clock[0] + 10000) or True)

    class Conn:
        def execute(self, sql):
            clock[0] += 100000 if "throughput_revision" in sql else 0
            row = (1, 2, 3, b"n", 5)
            return type("C", (), {"fetchone": lambda s: row if "throughput_revision" in sql else (9,)})()
    out = cpu.step(lambda: rev.read_stamp(Conn()))
    assert out == (9, 2, 3, b"n", 5)
    got = {k.split(">.", 1)[1]: v["cpu_s"] for k, v in marks(records).items()}
    assert got == {"stamp.shape": 1, "stamp.tables": 10, "stamp.triggers": 100,
                   "stamp.invariant": 1000, "stamp.row": 110000}


def test_read_stamp_short_circuits_like_the_original(measured, monkeypatch):
    _clock, records = measured
    called = []
    monkeypatch.setattr(rev, "_supported_shape", lambda c: called.append("shape") or False)
    monkeypatch.setattr(rev, "_tables_exact", lambda c: called.append("tables"))
    assert cpu.step(lambda: rev.read_stamp(object())) is None
    assert called == ["shape"]
    assert set(marks(records)) == {"sync.detail/<lambda>.stamp.shape"}


def test_real_stamp_chain_of_13_databases_loses_no_mark(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli import machine_throughput_metrics as mt
    paths = [tmp_path / "state.db"] + [tmp_path / "profiles" / f"p{i}" / "state.db" for i in range(12)]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        SessionDB(db_path=path).close()
    mt.reset_cache()
    loop_accounting.reset()
    monkeypatch.setattr(cpu, "_local", threading.local())
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: False)
    stamps = []
    real = rev.read_stamp
    monkeypatch.setattr(rev, "read_stamp", lambda conn: stamps.append(1) or real(conn))
    try:
        def machine_report():
            return mt.counters(tmp_path)
        assert cpu.step(machine_report)
        assert cpu.step(machine_report)  # warm: every database stamps again
        snap = loop_accounting.snapshot()["loops"]
    finally:
        mt.reset_cache()
    shape = snap["sync.detail/machine_report.stamp.shape"]
    assert len(stamps) >= 13 and shape["rows"] == len(stamps)
    for part in ("tables", "triggers", "invariant", "row"):
        assert snap[f"sync.detail/machine_report.stamp.{part}"]["rows"] == len(stamps)
    assert not getattr(cpu._local, "pending", None)


def test_open_chain_publishes_each_part_and_never_records_under_a_lock(tmp_path, monkeypatch):
    from hermes_cli import kanban_generation as gen
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setattr(cpu, "_local", threading.local())
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: False)
    loop_accounting.reset()
    real = loop_accounting.record
    held = []

    def record(name, **kw):
        if name.startswith("sync.detail/"):
            got = kbc._INIT_LOCK._is_owned()
            held.append((name, got, gen_lock_owned(gen)))
        return real(name, **kw)

    def gen_lock_owned(module):
        lock = getattr(module, "_lock", None)
        return bool(lock is not None and getattr(lock, "_is_owned", lambda: False)())
    monkeypatch.setattr(loop_accounting, "record", record)

    def acquire():
        conn = kbc.connect(home / "kanban.db")
        conn.close()
    cpu.step(acquire)
    names = {n for n, *_ in held}
    for part in ("acq.wal", "acq.guard2", "acq.pragmas"):
        assert f"sync.detail/acquire.{part}" in names
    assert not [h for h in held if h[1] or h[2]]
    for name in names:
        assert re.fullmatch(r"sync\.detail/[a-z_]+\.[a-z0-9_.]+", name), name


def test_dispatch_owner_open_marks_lifecycle_recover_connect_prepare(tmp_path, monkeypatch):
    from hermes_cli import kanban_dispatch_owner as owner
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    kbc.connect(home / "kanban.db").close()
    monkeypatch.setattr(cpu, "_local", threading.local())
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: False)
    loop_accounting.reset()

    def acquire():
        conn = owner.connect_with_identity(home / "kanban.db")
        try:
            return conn.execute("SELECT 1").fetchone()[0]
        finally:
            conn.close()
    assert cpu.step(acquire) == 1
    snap = loop_accounting.snapshot()["loops"]
    for part in ("acq.lifecycle", "acq.recover", "acq.connect_tracked", "acq.prepare",
                 "prepare.guard", "prepare.remember"):
        assert snap[f"sync.detail/acquire.{part}"]["rows"] >= 1, part
    assert snap["sync.detail/acquire.acq.lifecycle"]["rows"] == 2  # lifecycle access + tick lock
