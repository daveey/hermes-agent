"""The session list's board read answers as before while handing Python O(1) SQLite rows per board. [daveey]

``/api/profiles/sessions`` stamps external-wait cards from every board whose change signal moved
(``kanban_external_activity.stamp_boards`` -> ``read_session_external``). On the hub's busy board that is most
requests, and the read handed Python ~445 rows for ~140 parked cards: the parked rows and newest park events
(``_local_wait_rows``), the scheduled statuses (``kanban_external_watch._card_states``), the titles, and a
``PRAGMA table_info(tasks)`` (one row per column) in ``kanban_contained_wait.read``. Each row step releases the GIL;
in the Desktop backend (~48k context switches/s) each re-acquire can wait out the 5 ms switch interval, which made
this route's p95 ~3 s. Each test pins the old answer (a reference copy of the old per-row reader where the query
changed) and the reduced work (rows handed out do not grow with the number of parked cards).
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_activity as kea
from hermes_cli import kanban_wait_info as kwi
from hermes_cli.kanban_session_wait_rows import _describe_waits, _local_wait_rows
from tests.hermes_cli import test_kanban_external_watch as helpers

kanban_home, gh, clock = helpers.kanban_home, helpers.gh, helpers.clock
park, tick = helpers.park, helpers.tick


@pytest.fixture(autouse=True)
def _fresh():
    kea.reset_for_tests()
    yield
    kea.reset_for_tests()


class _Cursor:
    def __init__(self, cur, owner):
        self._cur, self._owner = cur, owner

    def __iter__(self):
        for row in self._cur:
            self._owner.rows += 1
            yield row

    def fetchall(self):
        rows = self._cur.fetchall()
        self._owner.rows += len(rows)
        return rows

    def fetchone(self):
        row = self._cur.fetchone()
        self._owner.rows += row is not None
        return row

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Counting:
    """Connection proxy counting statements and the result rows handed to Python."""

    def __init__(self, conn):
        self.conn, self.statements, self.rows = conn, 0, 0

    def execute(self, sql, params=()):
        self.statements += 1
        return _Cursor(self.conn.execute(sql, params), self)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _old_local_wait_rows(conn, task_ids):
    """``_local_wait_rows`` before: one row per parked card, newest park event grouped over ``idx_events_task``."""
    from hermes_cli.kanban_table_columns import table_columns

    ids = [str(t) for t in dict.fromkeys(task_ids)]
    cols = table_columns(conn, "tasks")
    pick = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in ("id", "status", "block_kind", "human_request",
                                                                          "awaiting"))
    parked = {}
    for tid, status, kind, request, awaiting in conn.execute(
            f"SELECT {pick} FROM tasks t WHERE t.id IN (SELECT value FROM json_each(?)) "
            "AND t.status IN ('scheduled', 'blocked', 'triage')", (json.dumps(ids),)):
        parked[str(tid)] = (status, kind, request, awaiting)
    if not parked:
        return {}, {}
    events = {}
    kph = ",".join("?" for _ in kwi.PARK_EVENT_KINDS)
    for tid, kind, payload, at in conn.execute(
            "SELECT e.task_id, e.kind, e.payload, e.created_at FROM task_events e WHERE e.id IN ("
            "SELECT MAX(id) FROM task_events WHERE task_id IN (SELECT value FROM json_each(?)) "
            f"AND kind IN ({kph}) GROUP BY task_id)", (json.dumps(list(parked)), *kwi.PARK_EVENT_KINDS)):
        events[str(tid)] = (kind, payload, at)
    return parked, events


def _board(n: int) -> sqlite3.Connection:
    """``n`` cards in every status, each with a mixed event history: several park kinds (the newest is not always
    the last event), non-park noise, REAL / integer / NULL ``created_at``, NULL and BLOB payloads, a card with no
    park event at all, and an id the board does not have."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, block_kind TEXT, human_request TEXT, "
        "awaiting TEXT, session_id TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER, "
        "kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);"
        "CREATE INDEX idx_events_task ON task_events(task_id, created_at);"
        "CREATE INDEX idx_events_kind_task_id ON task_events(kind, task_id, id);")
    statuses = ("scheduled", "blocked", "triage", "running", "done", "todo")
    park_kinds = kwi.PARK_EVENT_KINDS
    for i in range(n):
        tid = f"t{i:03d}"
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", (
            tid, f"card {i}", statuses[i % len(statuses)], ("time", None, "needs_input", "dependency")[i % 4],
            json.dumps({"ask": f"q{i}"}) if i % 5 == 0 else None,
            json.dumps({"kind": ("ci", "deploy", "clock")[i % 3], "ref": f"r{i}", "why": "w"}) if i % 2 else None,
            f"s{i % 3}"))
        if i % 7 == 3:
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'heartbeat', '{}', 1)",
                         (tid,))
            continue  # parked or not, no park event: the old GROUP BY emits nothing for it
        for j in range(6 + i % 4):
            kind = park_kinds[(i + j) % len(park_kinds)]
            at = (1_700_000_000 + i * 100 + j + 0.123456789123) if (i + j) % 3 == 0 else 1_700_000_000 + i * 100 + j
            payload: object = json.dumps({"reason": f"r{i}-{j}", "awaiting": {"kind": "ci", "ref": f"r{i}"}})
            if j % 5 == 4:
                payload = None
            if (i, j) == (8, 5):  # t008's newest park event (triage)
                payload = sqlite3.Binary(b'{"reason": "blob"}')
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                         (tid, kind, payload, at))
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'comment', 'x', ?)",
                         (tid, at))
    conn.commit()
    return conn


def _ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM tasks ORDER BY id DESC")] + ["missing"]


@pytest.mark.parametrize("factory", [None, sqlite3.Row])
def test_wait_rows_answer_exactly_as_before_order_and_types_included(factory):
    conn = _board(60)
    conn.row_factory = factory
    ids = _ids(conn)
    new, old = _local_wait_rows(conn, ids), _old_local_wait_rows(conn, ids)
    assert new == old and repr(list(new[0].items())) == repr(list(old[0].items()))
    assert repr(sorted(new[1].items())) == repr(sorted(old[1].items()))
    assert any(isinstance(at, float) for _k, _p, at in new[1].values())
    assert any(isinstance(p, bytes) for _k, p, _a in new[1].values())
    now = 1_700_009_999.0
    assert repr(_describe_waits(*new, now=now)) == repr(_describe_waits(*old, now=now))


def test_wait_rows_hand_out_the_same_rows_for_any_number_of_parked_cards():
    work = []
    for n in (12, 120):
        conn = Counting(_board(n))
        parked, events = _local_wait_rows(conn, _ids(conn.conn))
        assert len(parked) >= n // 2 and len(events) >= n // 3
        work.append((conn.statements, conn.rows))
    # Old reader: one row per parked card and one per newest park event (~180 rows for 120 cards).
    assert work[0] == work[1] and work[1][1] <= 4, work


def test_wait_rows_probe_the_newest_park_event_through_the_kind_index(monkeypatch):
    import hermes_state_page_pack

    conn = _board(30)
    seen: list[tuple[str, tuple]] = []
    real = hermes_state_page_pack.read_rows

    def spy(c, sql, params, width):
        seen.append((sql, tuple(params)))
        return real(c, sql, params, width)

    monkeypatch.setattr(hermes_state_page_pack, "read_rows", spy)
    _local_wait_rows(conn, _ids(conn))
    (sql, params), = [(s, p) for s, p in seen if "FROM task_events e" in s]
    plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    # Not idx_events_task (task_id, created_at), which walked every event of each card.
    assert "idx_events_kind_task_id" in plan and "idx_events_task " not in plan + " ", plan


def test_a_board_without_task_events_keeps_status_only_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('a', 'blocked'), ('b', 'running')")
    assert _local_wait_rows(conn, ["a", "b"]) == ({"a": ("blocked", None, None, None)}, {})


def test_contained_read_needs_no_schema_probe():
    from hermes_cli import kanban_contained_wait

    conn = _board(10)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    assert kanban_contained_wait.read(conn, _ids(conn)) == {}  # a board without containment_episode
    assert kanban_contained_wait.read(sqlite3.connect(":memory:"), ["a"]) == {}  # nor a tasks table
    conn.execute("ALTER TABLE tasks ADD COLUMN containment_episode TEXT")
    assert kanban_contained_wait.read(conn, _ids(conn)) == {}
    # The old read probed ``PRAGMA table_info(tasks)``: one row per column, on every board read.
    assert not [s for s in statements if "table_info" in s.lower()], statements


def _external_board(conn, n):
    tids = [park(conn, 9000 + i) for i in range(n)]
    return [(f"s{i % 3}", t) for i, t in enumerate(tids)]


def test_session_external_answers_as_before_and_hands_out_o1_rows(kanban_home, gh, clock, monkeypatch):
    from hermes_state_page_pack import read_rows as packed

    def unpacked(conn, sql, params, width):
        """The transport before: the same statement, one Python row per result row."""
        return [tuple(row) for row in conn.execute(sql, params)]

    work = []
    with kbc.connect_closing() as conn:
        pairs = _external_board(conn, 4)
        tick(conn, gh, clock, 0)
        for extra in (0, 12):
            if extra:
                pairs += _external_board(conn, extra)
                tick(conn, gh, clock)
            now = clock.now + 1
            counted = Counting(conn)
            got = kea.read_session_external(counted, pairs, now, strict=False)
            work.append(counted.rows)
            monkeypatch.setattr("hermes_state_page_pack.read_rows", unpacked)
            try:
                assert json.dumps(kea.read_session_external(conn, pairs, now, strict=False)) == json.dumps(got)
            finally:
                monkeypatch.setattr("hermes_state_page_pack.read_rows", packed)
            assert sum(slot["counts"][0] for slot in got.values()) == len(pairs)
            assert all(card["title"].startswith("card ") for slot in got.values() for card in slot["cards"])
    # 4 cards vs 16 cards: the same rows handed out (the old read grew by ~4 rows per card).
    assert work[0] == work[1], work


def test_card_states_keep_only_scheduled_cards(kanban_home, gh, clock):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_external_watch as kew

    with kbc.connect_closing() as conn:
        a, b = park(conn, 9101), park(conn, 9102)
        tick(conn, gh, clock, 0)
        other = kb.create_task(conn, title="plain", assignee="worker")
        counted = Counting(conn)
        got = kew.card_states(counted, [a, other, "missing", b], now=time.time())
        assert set(got) == {a, b}


def test_read_rows_is_fetchall_in_one_statement_and_one_row():
    from hermes_state_page_pack import read_rows

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE v (a, b, c)")
    values = [(1, "x", None), (2.5, b"\x00\xff", 0.1 + 0.2), (-7, "", 1_700_000_000.123456789),
              (2**62, "é\"'\\", float("inf")), (3, None, 1e-300)]
    conn.executemany("INSERT INTO v VALUES (?, ?, ?)", values)
    sql, params = "SELECT a, b, c FROM v WHERE a != ? ORDER BY rowid DESC", (3,)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    got = read_rows(conn, sql, params, 3)
    conn.set_trace_callback(None)
    want = conn.execute(sql, params).fetchall()
    assert got == want and [tuple(map(type, r)) for r in got] == [tuple(map(type, r)) for r in want]
    assert len(statements) == 1
    assert read_rows(conn, "SELECT a FROM v WHERE 0", (), 1) == []
    with pytest.raises(sqlite3.Error):
        read_rows(conn, sql, params, 2)  # a wrong width fails at prepare, never a silent reshape
