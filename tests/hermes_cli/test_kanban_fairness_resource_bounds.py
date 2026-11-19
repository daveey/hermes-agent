"""The fairness journal bounds UTF-8 bytes of the stored fields before admitting a row, in the opening snapshot
and in the triggers, and keeps the record bound. [daveey]"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_fairness_journal as journal


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as handle:
        yield handle


def _utf8_total(conn) -> int:
    return sum(len(r[5].encode("utf-8")) for r in journal.rows(conn, limit=10**9))


def _count(conn, kind=None) -> int:
    sql = f"SELECT COUNT(*) FROM {journal.JOURNAL}" + (" WHERE kind = ?" if kind else "")
    return conn.execute(sql, (kind,) if kind else ()).fetchone()[0]


def _seed(conn, keys):
    return [kb.create_task(conn, title=f"t{i}", assignee="a", idempotency_key=key) for i, key in enumerate(keys)]


@pytest.mark.parametrize("key", ["a" * 70_000, "界" * 30_000], ids=["70k-ascii", "30k-unicode"])
def test_opening_1000_rows_ascii_and_unicode_keys_stay_in_byte_bound(conn, key):
    _seed(conn, [str(i) + key for i in range(1000)])
    bound = 64 * 1024 * 1024
    state = journal.arm(conn, "c1", max_bytes=bound)
    assert state["bytes"] == _utf8_total(conn)
    assert state["bytes"] <= bound
    assert state["stored"] == _count(conn, "opening") < 1000
    assert state["dropped"] == 1000 - state["stored"] > 0
    assert state["first_drop_ms"] == state["last_drop_ms"] == state["armed_ms"]


def test_segment_ceiling_checks_before_append(tmp_path):
    from hermes_cli import kanban_fairness_segments as segments
    from hermes_cli.kanban_fairness_schema import make_record, SEGMENT_BYTES

    def records():
        for seq in range(1, 131):
            yield make_record(capture_id="cap", source_gateway="gw", board_uid="board",
                              source_epoch="epoch", seq=seq, kind="opening", wall_ns=1,
                              mono_ns=1, clock_error_ns=0,
                              payload={"task_id": str(seq), "op": None,
                                       "fields": {"status": "ready", "idempotency_key": "a" * 70_000},
                                       "journal_seq": seq, "journal_kind": "opening",
                                       "database_uid": "db", "source_uid": "uid" + str(seq)})
    receipt = segments.append_records(tmp_path, records())
    assert len(receipt["segments"]) == 2
    assert all((tmp_path / item["name"]).stat().st_size <= SEGMENT_BYTES for item in receipt["segments"])
    segments.seal_records(tmp_path, expected_sources=receipt["sources"])
    loaded = segments.load_records(tmp_path)
    assert len(loaded["records"]) == 130 and loaded["gaps"] == []


def test_opening_unicode_counts_bytes_not_characters(conn):
    _seed(conn, ["é" * 2000 + str(i) for i in range(10)])  # ~4k bytes each, ~2k characters
    journal.arm(conn, "probe")
    first = next(journal.rows(conn))[5]
    one, chars = len(first.encode("utf-8")), len(first)
    assert one > chars
    bound = one * 4 + 100
    state = journal.arm(conn, "c2", max_bytes=bound, replace=True)
    assert state["stored"] == 4 and state["dropped"] == 6
    assert state["bytes"] == _utf8_total(conn) <= bound


def test_streaming_trigger_never_overshoots_64mib(conn):
    bound = 64 * 1024 * 1024
    journal.arm(conn, "c3")
    big = "é" * 400_000  # 800k bytes, 400k characters
    created = 0
    while created < 100:  # 100 * ~0.8 MiB > 64 MiB
        kb.create_task(conn, title="t", assignee="a", idempotency_key=big + str(created))
        created += 1
    state = journal.status(conn)
    assert state["bytes"] == _utf8_total(conn) <= bound
    assert _count(conn, "task") < created and state["dropped"] > 0
    assert state["first_drop_ms"] is not None and state["last_drop_ms"] >= state["first_drop_ms"]


def test_trigger_counts_utf8_bytes_with_small_bound(conn):
    journal.arm(conn, "c4", max_bytes=3000)
    for i in range(6):
        kb.create_task(conn, title="t", assignee="a", idempotency_key="é" * 600 + str(i))  # >1200 bytes
    state = journal.status(conn)
    assert state["bytes"] == _utf8_total(conn) <= 3000
    assert _count(conn, "task") < 6 and state["dropped"] > 0


def test_oversized_individual_rows_are_gaps_and_later_small_rows_admit(conn):
    journal.arm(conn, "c5", max_bytes=4000)
    kb.create_task(conn, title="huge", assignee="a", idempotency_key="x" * 10_000)
    assert _count(conn, "task") == 0 and journal.status(conn)["dropped"] >= 1
    kb.create_task(conn, title="small", assignee="a")
    state = journal.status(conn)
    assert _count(conn, "task") == 1 and state["bytes"] == _utf8_total(conn) <= 4000


def test_opening_skips_oversized_row_and_keeps_the_rest(conn):
    _seed(conn, ["x" * 10_000, None, None])
    state = journal.arm(conn, "c6", max_bytes=4000)
    assert state["stored"] == 2 and state["dropped"] == 1
    assert state["bytes"] == _utf8_total(conn) <= 4000
    assert all(json.loads(r[5])["idempotency_key"] is None for r in journal.rows(conn))


def test_record_bound_preserved_at_100k(conn):
    state = journal.arm(conn, "c7")
    assert state["max_records"] == 100_000 and state["max_bytes"] == 64 * 1024 * 1024
    journal.arm(conn, "c8", max_records=3, replace=True)
    for _ in range(5):
        kb.create_task(conn, title="t", assignee="a")
    state = journal.status(conn)
    assert state["stored"] == 3 and state["dropped"] >= 2  # task rows plus their created events
    assert state["bytes"] == _utf8_total(conn)
