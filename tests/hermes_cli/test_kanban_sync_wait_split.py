"""The wait step's live CPU split: sync.wait/* partitions sync.step/...wait and adds nothing. [daveey]"""
import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_cpu as cpu
from hermes_cli import kanban_sync_timing as timing
from hermes_cli import loop_accounting
from hermes_cli.kanban_sync_board_watch import BoardWatch

STEP = "sync.step/hermes_cli.kanban_sync_timing.wait"
PARTS = ("boundary", "event_wait", "outbox_probe", "reader_open")
TICKS = 3


class _Wake:
    """An event whose wait costs CPU and never fires."""

    def __init__(self, spend):
        self._spend = spend

    def wait(self, timeout=None):
        self._spend(0.004)
        return False

    def clear(self):
        pass


def _spin(seconds):
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def _run_wait(monkeypatch, spend, *, split=True):
    """One loop round whose wait step runs TICKS ticks; the first probe opens a reader."""
    loop_accounting.reset()
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: True)
    monkeypatch.setattr(cpu, "_local", threading.local())
    monkeypatch.setattr(timing._DIAG, "boundary", lambda: spend(0.002))
    if not split:
        monkeypatch.setattr(cpu.WaitSplit, "publish", lambda self: None)
        monkeypatch.setattr(cpu, "wait_boundary", lambda fn: fn())
        monkeypatch.setattr(cpu, "note_reader_open", lambda cpu_s, reason: None)
    left = [TICKS]

    def probe(paths):
        if left[0] == TICKS:
            spend(0.003)
            cpu.note_reader_open(0.003, "changed")  # what BoardWatch does after a reopen
        spend(0.001)
        left[0] -= 1
        return False

    monkeypatch.setattr(client, "outbox_pending", probe)
    with cpu.iterations(lambda: True) as rounds:
        next(rounds)
        cpu.step(timing.wait, client.wait_for_work, _Wake(spend), [], 60.0, lambda: left[0] > 0)
    stats = loop_accounting.counters()
    accounted = sum(loop_accounting._copy_accounted().values())
    loop_accounting.reset()
    return stats, accounted


def _non_board(stats):
    return stats["sync.cpu.total/loop"]["cpu_s"] - sum(
        v["cpu_s"] for k, v in stats.items() if k.startswith("sync.board/"))


@pytest.fixture
def fake_clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "thread_time", lambda: now[0])
    monkeypatch.setattr(loop_accounting, "_thread_time", lambda: now[0])
    return lambda seconds: now.__setitem__(0, now[0] + seconds)


def test_wait_split_sums_to_step_and_adds_nothing(monkeypatch, fake_clock):
    stats, accounted = _run_wait(monkeypatch, fake_clock)
    parts = {p: stats[f"sync.wait/{p}"]["cpu_s"] for p in PARTS}
    assert parts == pytest.approx({"boundary": 0.002, "event_wait": 0.004 * TICKS,
                                   "outbox_probe": 0.001 * TICKS, "reader_open": 0.003})
    assert stats["sync.wait/event_wait"]["rows"] == TICKS
    step = stats[STEP]["cpu_s"]
    assert step > 0 and abs(sum(parts.values()) - step) <= 0.02 * step
    assert stats["sync.reader_open.step/wait"]["cpu_s"] == pytest.approx(0.003)
    assert stats["sync.reader_open.reason/changed"]["runs"] == 1
    assert not [k for k in stats if k.startswith("sync.step/") and k != STEP]

    plain, plain_accounted = _run_wait(monkeypatch, fake_clock, split=False)
    assert not [k for k in plain if k.startswith(("sync.wait/", "sync.reader_"))]
    assert stats[STEP]["cpu_s"] == pytest.approx(plain[STEP]["cpu_s"])
    assert _non_board(stats) == pytest.approx(_non_board(plain))
    assert accounted == pytest.approx(plain_accounted)
    shared = [k for k in plain if k.startswith("sync.")]
    assert {k: stats[k]["cpu_s"] for k in shared} == pytest.approx({k: plain[k]["cpu_s"] for k in shared})


def test_wait_split_sums_to_step_on_the_real_clock(monkeypatch):
    """Real thread CPU: what the marks leave out (the step's own bookkeeping) stays under 2%."""
    stats, _ = _run_wait(monkeypatch, lambda seconds: _spin(seconds * 5))
    step = stats[STEP]["cpu_s"]
    total = sum(stats[f"sync.wait/{p}"]["cpu_s"] for p in PARTS)
    assert step > 0.08 and total <= step and step - total <= 0.02 * step


def test_reader_opens_are_booked_under_the_step_that_triggered_them(tmp_path, monkeypatch):
    loop_accounting.reset()
    monkeypatch.setattr(cpu, "_local", threading.local())
    path = tmp_path / "board.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v)")
    conn.commit()
    conn.close()
    watch = BoardWatch()

    def select():
        return watch.query(path, "SELECT count(*) FROM t")

    try:
        assert cpu.step(select) is not None
        watch.reset()
        with cpu.service("default"):
            assert select() is not None
        watch.reset()
        assert select() is not None
    finally:
        watch.reset()
    stats = loop_accounting.counters()
    assert {k: v["runs"] for k, v in stats.items() if k.startswith("sync.reader_open.")} == {
        "sync.reader_open.step/select": 1, "sync.reader_open.step/board": 1,
        "sync.reader_open.step/other": 1, "sync.reader_open.reason/first": 3}
    loop_accounting.reset()
