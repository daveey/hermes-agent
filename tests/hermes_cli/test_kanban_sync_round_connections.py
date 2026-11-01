"""Round-scoped replica lifetime: no second schema/identity setup after pulling."""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_partition_repair as partition
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_sync_idle_round import _round, _settle


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, "default", "macbook")
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_round_connection_replay(live, svc, kanban_home, monkeypatch, mode):
    parent, _ = _settle(live, svc, kanban_home, mode)
    for _ in range(3):
        _round(kanban_home, svc)
    stats = {}

    def instrument(module, name):
        real = getattr(module, name)
        def wrapped(*args, **kwargs):
            start = time.thread_time_ns()
            try:
                return real(*args, **kwargs)
            finally:
                row = stats.setdefault(name, {"calls": 0, "cpu_ns": 0})
                row["calls"] += 1
                row["cpu_ns"] += time.thread_time_ns() - start
        monkeypatch.setattr(module, name, wrapped)

    instrument(ksc, "open_shadow")
    instrument(partition, "repair_partition_links")
    rows = []
    for changed in (False, True):
        stats.clear()
        start = time.thread_time_ns()
        for i in range(10):
            if changed:
                with kbc.write_txn(live):
                    live.execute("UPDATE tasks SET title = ? WHERE id = ?", (f"changed-{i}", parent))
            _round(kanban_home, svc)
        rows.append({"changed": changed, "cpu_ns": time.thread_time_ns() - start,
                     "paths": {k: dict(v) for k, v in stats.items()}})
    print("ROUND_REPLAY", json.dumps({"mode": mode, "rounds": rows}, sort_keys=True))
    assert rows[0]["paths"].get("repair_partition_links", {}).get("calls", 0) == 0
    assert rows[1]["paths"]["repair_partition_links"]["calls"] == 10
    assert svc.board("default")["rows"]["tasks"][parent]["title"] == "changed-9"
    assert all(row["paths"]["open_shadow"]["calls"] == 10 for row in rows)


@pytest.mark.parametrize("failure", [None, "pull", "repair"])
def test_round_closes_replica_on_success_and_failure(live, svc, kanban_home, monkeypatch, failure):
    _settle(live, svc, kanban_home, "primary")
    opened = []
    real = ksc.open_shadow

    def opening(*args):
        conn = real(*args)
        opened.append(conn)
        return conn

    def fail(*args, **kwargs):
        raise RuntimeError("injected round failure")

    monkeypatch.setattr(ksc, "open_shadow", opening)
    if failure == "pull":
        monkeypatch.setattr(ksc, "pull", fail)
    elif failure == "repair":
        ksc._REPAIRED.clear()
        monkeypatch.setattr(partition, "repair_partition_links", fail)
    if failure:
        with pytest.raises(RuntimeError, match="injected round failure"):
            _round(kanban_home, svc)
    else:
        _round(kanban_home, svc)
    assert len(opened) == 1
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_resync_replacement_is_used_for_repairs_and_closed(live, svc, kanban_home, monkeypatch):
    _settle(live, svc, kanban_home, "primary")
    svc.compact("default")
    svc.board("default")["floor"] += 1
    opened = []
    repaired = []
    real_open, real_repair = ksc.open_shadow, partition.repair_partition_links

    def opening(*args):
        conn = real_open(*args)
        opened.append(conn)
        return conn

    def repair(live, replica, gateway):
        repaired.append(replica)
        assert replica.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        return real_repair(live, replica, gateway)

    monkeypatch.setattr(ksc, "open_shadow", opening)
    monkeypatch.setattr(partition, "repair_partition_links", repair)
    ksc._REPAIRED.clear()
    _round(kanban_home, svc)
    assert len(opened) == 2  # old replica plus replacement, no third connection
    assert repaired == [opened[1]]
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
