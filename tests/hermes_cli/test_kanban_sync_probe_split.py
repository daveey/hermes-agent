"""Nested sync diagnostics must preserve selection, restore fences and outer CPU."""
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_sync_cpu as cpu
from hermes_cli import kanban_sync_board_watch as watch_module
from hermes_cli import loop_accounting
from hermes_cli.kanban_sync_work import SyncWork


@pytest.fixture
def accounting(monkeypatch):
    loop_accounting.reset()
    monkeypatch.setattr(cpu, "_local", threading.local())
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: False)
    yield
    loop_accounting.reset()


def test_select_probe_records_only_executed_calls(accounting):
    work = SyncWork(10)
    boards = [(s, Path(s)) for s in ("dirty", "quiet", "pending", "blocked")]
    assert work.select(boards, 1, lambda paths: False) == boards
    work.changed("dirty")
    work.finished("blocked", False, now=1)
    calls = []

    def pending(paths):
        calls.append(paths)
        return paths == [Path("pending")]

    assert cpu.step(work.select, boards, 1.5, pending) == [boards[0], boards[2]]
    assert calls == [[Path("quiet")], [Path("pending")]]
    stats = loop_accounting.counters()
    assert stats["sync.detail/select.outbox_probe"]["runs"] == 2
    assert not [k for k in stats if k.startswith(("sync.step/", "sync.board/"))]
    assert sum(loop_accounting._copy_accounted().values()) == 0


def test_select_probe_records_exception_and_restores_step(accounting):
    work = SyncWork(10)
    boards = [("one", Path("one"))]
    work.select(boards, 1, lambda paths: False)
    error = ValueError("probe")

    def pending(paths):
        raise error

    with pytest.raises(ValueError) as caught:
        cpu.step(work.select, boards, 2, pending)
    assert caught.value is error
    cpu.note_reader_keep(0.25)
    stats = loop_accounting.counters()
    assert stats["sync.detail/select.outbox_probe"]["runs"] == 1
    assert stats["sync.reader_keep.step/other"]["cpu_s"] == 0.25
    assert sum(loop_accounting._copy_accounted().values()) == 0


def test_keep_proof_parts_and_step_cpu_are_nonadditive(accounting, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(cpu.time, "thread_time", lambda: now[0])
    monkeypatch.setattr(loop_accounting, "_thread_time", lambda: now[0])
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: True)
    watch = watch_module.BoardWatch()
    identity = (1, 2, 3, 4)

    class Reader:
        def execute(self, sql):
            assert sql == "PRAGMA shrink_memory"
            now[0] += 0.2

    reader = Reader()
    ent = (identity, reader, 1, "old", 1, "path", ["schema"])
    watch._conns["path"] = ent
    watch._answers["path"] = {"query": (1, "old")}

    def read(path):
        assert path == "path"
        now[0] += 0.1
        return "new"

    def parsed(conn, path):
        assert conn is reader and path == "path"
        now[0] += 0.3
        return ["schema"]

    monkeypatch.setattr(watch_module._wal_evidence, "read", read)
    monkeypatch.setattr(watch_module._wal_evidence, "checkpointed", lambda a, b: (a, b) == ("old", "new"))
    monkeypatch.setattr(watch_module, "parsed_from", parsed)

    def select():
        assert watch._kept_over_checkpoint("path", ent, (1, 2, 5, 4))

    with cpu.service("loop"):
        cpu.step(select)
    stats = loop_accounting.counters()
    expected = {"checkpoint_wal": 0.1, "checkpoint_shrink": 0.2, "checkpoint_schema": 0.3}
    for part, seconds in expected.items():
        assert stats[f"sync.detail/select.{part}"]["cpu_s"] == pytest.approx(seconds)
    assert stats["sync.reader_keep.step/select"]["cpu_s"] == pytest.approx(0.6)
    assert stats["sync.reader_keep/checkpoint"]["cpu_s"] == pytest.approx(0.6)
    assert stats["sync.cpu.total/loop"]["cpu_s"] == pytest.approx(0.6)
    assert sum(loop_accounting._copy_accounted().values()) == pytest.approx(0.6)
    assert len([k for k in stats if k.startswith("sync.step/")]) == 1
    assert not [k for k in stats if k.startswith("sync.board/")]
    assert "path" not in watch._answers
    assert watch._conns["path"][1] is reader
