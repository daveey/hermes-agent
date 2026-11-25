"""Expiry is judged after I/O, using genuine decision timestamps. [daveey]"""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_governors as governors
from hermes_cli import kanban_why_fast as fast
from tests.hermes_cli import test_kanban_governors as helpers

board = helpers.board
clean = helpers.clean


@pytest.mark.parametrize("reader", ["fast", "snapshot", "card"])
@pytest.mark.parametrize("delay,stale", [(2, False), (4, True)])
def test_read_uses_clock_after_snapshot_io(board, monkeypatch, reader, delay, stale):
    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="expiry during read", assignee="default")
        helpers._tick(conn)
        path = governors._db_path(conn)
        snap_path = governors.snapshot_path(conn)
        doc = json.loads(snap_path.read_text())
        doc.update(tick_at=1000, freshness_bound_s=90)
        snap_path.write_text(json.dumps(doc))
        clock = [1087.0]
        read_text = type(snap_path).read_text

        def delayed_read(self, *args, **kwargs):
            value = read_text(self, *args, **kwargs)
            if self == snap_path:
                clock[0] += delay
            return value

        monkeypatch.setattr(type(snap_path), "read_text", delayed_read)
        clock_module = SimpleNamespace(time=lambda: clock[0], monotonic=fast.time.monotonic)
        monkeypatch.setattr(fast, "time", clock_module)
        monkeypatch.setattr(governors, "time", clock_module)
        if reader == "fast":
            result = fast.read_answer(path, tid)
            if stale:
                assert result is None
                return
        else:
            result = governors.snapshot(conn) if reader == "snapshot" else governors.card(conn, tid)
            assert result["stale"] is stale
        assert result["age_s"] == 87 + delay
        assert (result.get("recorded") or result)["tick_at"] == 1000


def test_fast_read_rechecks_expiry_after_status_io(board, monkeypatch):
    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="expiry during status lookup", assignee="default")
        helpers._tick(conn)
        path = governors._db_path(conn)
        snap_path = governors.snapshot_path(conn)
        doc = json.loads(snap_path.read_text())
        doc.update(tick_at=1000, freshness_bound_s=90)
        snap_path.write_text(json.dumps(doc))
        clock = [1089.0]
        monkeypatch.setattr(fast, "time", SimpleNamespace(time=lambda: clock[0], monotonic=fast.time.monotonic))
        from hermes_cli import kanban_lockguard
        connect = kanban_lockguard.connect

        def delayed_connect(*args, **kwargs):
            result = connect(*args, **kwargs)
            clock[0] += 2
            return result

        monkeypatch.setattr(kanban_lockguard, "connect", delayed_connect)
        assert fast.read_answer(path, tid) is None


@pytest.mark.parametrize("reader", ["fast", "snapshot", "card"])
def test_publication_after_call_start_is_not_from_the_future(board, monkeypatch, reader):
    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="publication overlaps reader", assignee="default")
        helpers._tick(conn)
        path = governors._db_path(conn)
        snap_path = governors.snapshot_path(conn)
        doc = json.loads(snap_path.read_text())
        # The recorder's genuine tick is published while this read is in progress.
        doc.update(tick_at=1001, freshness_bound_s=90)
        snap_path.write_text(json.dumps(doc))
        clock = [1000.0]
        read_text = type(snap_path).read_text

        def overlapping_read(self, *args, **kwargs):
            if self == snap_path:
                clock[0] = 1002.0
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(type(snap_path), "read_text", overlapping_read)
        clock_module = SimpleNamespace(time=lambda: clock[0], monotonic=fast.time.monotonic)
        monkeypatch.setattr(fast, "time", clock_module)
        monkeypatch.setattr(governors, "time", clock_module)
        if reader == "fast":
            result = fast.read_answer(path, tid)
            assert result is not None
        else:
            result = governors.snapshot(conn) if reader == "snapshot" else governors.card(conn, tid)
            assert result["stale"] is False
        assert result["age_s"] == 1


def test_run11_expiry_replay_falls_back_then_reads_new_generation(board, monkeypatch):
    helpers._usage(monkeypatch, [helpers.RESERVE + 7])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="retained run11 boundary", assignee="default")
        helpers._tick(conn)
        path = governors._db_path(conn)
        snap_path = governors.snapshot_path(conn)
        doc = json.loads(snap_path.read_text())
        tick = 1795615194.9005919
        query = 1795615300.0851958
        bound = 102.57679677009583
        doc.update(tick_at=tick, freshness_bound_s=bound)
        snap_path.write_text(json.dumps(doc))
        assert fast.read_answer(path, tid, now=tick + bound) is not None
        assert fast.read_answer(path, tid, now=tick + bound + .001) is None
        assert fast.read_answer(path, tid, now=query) is None
        # No claim about run11's historical import timing: reproduce only routing.
        fresh_tick = 1795615303.101069
        doc.update(tick_at=fresh_tick, freshness_bound_s=120.53296732902527)
        snap_path.write_text(json.dumps(doc))
        monkeypatch.setattr(governors, "time", SimpleNamespace(time=lambda: fresh_tick + 6.2))
        from hermes_cli import kanban_governors_cli as cli
        result = cli.why(conn, tid)
        assert result["source"] == "snapshot"
        assert result["recorded"]["tick_at"] == fresh_tick
        assert result["age_s"] == 6.2
        assert fast.read_answer(path, tid, now=fresh_tick + 601) is None
