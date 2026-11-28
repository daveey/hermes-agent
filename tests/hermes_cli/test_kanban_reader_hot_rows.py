"""Kanban readers answer as before while handing Python far fewer SQLite rows. [daveey]

In the Desktop backend every statement prepare and row step releases the GIL and may wait out a 5 ms switch
interval, so each reader below must keep its answer and do its filtering/aggregation inside SQLite. Each test pins
the old answer (a reference copy of the old per-row reader) and the reduced work (statements and rows handed out).
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_external_watch as kew
from hermes_cli import kanban_session_activity_card_details as details
from hermes_cli.kanban_run_model import _KINDS, _concrete, _payload, add_run_models
from hermes_cli.kanban_table_columns import table_columns, tables_columns


class _Counted:
    """A cursor that counts the rows it hands out."""

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


class Counting:
    """Connection proxy counting statements and result rows (``execute`` only, as the readers use it)."""

    def __init__(self, conn):
        self.conn, self.statements, self.rows = conn, 0, 0

    def execute(self, sql, params=()):
        self.statements += 1
        return _Counted(self.conn.execute(sql, params), self)

    def __getattr__(self, name):
        return getattr(self.conn, name)


# --- schema probe ---------------------------------------------------------------------------------------------

def _pragma(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_table_columns_match_the_pragma_in_one_statement(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "b.db")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, " + ", ".join(f"c{i} TEXT" for i in range(60)) + ")")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    got = tables_columns(conn, "tasks", "task_runs", "missing")
    conn.set_trace_callback(None)
    # One statement (and one result row) for three tables, 63 columns; ``--`` lines trace the pragma virtual tables
    # SQLite runs inside it.
    assert len([s for s in statements if not s.startswith("--")]) == 1
    assert got == (frozenset(_pragma(conn, "tasks")), frozenset(_pragma(conn, "task_runs")), frozenset())
    assert table_columns(conn, "tasks") == _pragma(conn, "tasks") and len(_pragma(conn, "tasks")) == 61


@pytest.mark.parametrize("factory", [None, sqlite3.Row, lambda cur, row: dict(zip([d[0] for d in cur.description], row))])
def test_table_columns_ignore_the_connection_row_factory(factory):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT, session_id TEXT)")
    conn.row_factory = factory
    assert table_columns(conn, "tasks") == {"id", "session_id"}


def test_table_columns_follow_schema_changes_and_file_swaps(tmp_path: Path):
    """No memo: a migration on this or another connection, or another file at the same path, is read as it is."""
    path = tmp_path / "b.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT)")
    conn.commit()
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert table_columns(reader, "tasks") == {"id"}
    conn.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
    conn.commit()
    assert table_columns(reader, "tasks") == {"id", "session_id"}
    reader.close()
    conn.close()
    path.unlink()
    other = sqlite3.connect(path)
    other.execute("CREATE TABLE tasks (id TEXT, assignee TEXT)")
    other.commit()
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert table_columns(reader, "tasks") == {"id", "assignee"}


def test_table_columns_resolve_like_the_pragma_temp_then_main_then_attached(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "main.db")
    conn.execute(f"ATTACH DATABASE '{tmp_path / 'aux.db'}' AS aux")
    conn.execute("CREATE TABLE aux.only_aux (a TEXT, b TEXT)")
    conn.execute("CREATE TABLE tasks (id TEXT)")
    assert table_columns(conn, "only_aux") == _pragma(conn, "only_aux") == {"a", "b"}
    conn.execute("CREATE TEMP TABLE tasks (id TEXT, shadow TEXT)")
    assert table_columns(conn, "tasks") == _pragma(conn, "tasks") == {"id", "shadow"}


# --- add_run_models -------------------------------------------------------------------------------------------

def _old_run_models(conn, found):
    """The reader before: every run's events of these kinds, newest run picked in Python."""
    ids = list(found)
    ph = ",".join("?" for _ in ids)
    newest = dict(conn.execute(f"SELECT task_id, MAX(id) FROM task_runs WHERE task_id IN ({ph}) GROUP BY task_id",
                               ids).fetchall())
    got = conn.execute(f"SELECT task_id, run_id, kind, payload FROM task_events WHERE task_id IN ({ph}) "
                       f"AND kind IN ({','.join('?' for _ in _KINDS)}) ORDER BY id", [*ids, *_KINDS]).fetchall()
    concrete, routed = {}, {}
    for task_id, run_id, kind, raw in got:
        if run_id is None or run_id != newest.get(task_id):
            continue
        payload = _payload(raw)
        if kind == "run_route":
            routed[task_id] = str(payload.get("model") or "").strip() or None
        elif model := _concrete(payload):
            concrete[task_id] = model
    return {t: concrete.get(t) or routed.get(t) for t in newest if concrete.get(t) or routed.get(t)}


def test_run_models_match_the_old_reader_and_read_only_the_newest_runs_events():
    rng = random.Random(7)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, "
                 "payload TEXT, created_at INTEGER)")
    conn.execute("CREATE INDEX idx_events_run ON task_events(run_id, id)")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    models = ["class:deep", "class:standard", "gpt-6-astra", "", None]
    run_id = 0
    for t in range(40):
        for _ in range(rng.randint(0, 25)):
            run_id += 1
            conn.execute("INSERT INTO task_runs (id, task_id) VALUES (?, ?)", (run_id, f"t{t}"))
            for _ in range(rng.randint(0, 4)):
                kind = rng.choice([*_KINDS, "heartbeat", "commented"])
                payload = ({"model": rng.choice(models)} if kind == "run_route" else
                           {"session_models": rng.sample(["a", "b", " ", "c"], rng.randint(0, 3))})
                conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?, ?, ?, ?)",
                             (f"t{t}", rng.choice([run_id, run_id, None, max(1, run_id - 1)]), kind,
                              rng.choice([json.dumps(payload), "{bad", None])))
    found = {f"t{t}": {"id": f"t{t}"} for t in range(45)}
    old = _old_run_models(conn, found)
    counted = Counting(conn)
    add_run_models(counted, found)
    assert {t: c["run_model"] for t, c in found.items() if "run_model" in c} == old
    newest_rows = conn.execute(
        f"SELECT COUNT(*) FROM task_events e WHERE e.kind IN ({','.join('?' for _ in _KINDS)}) "
        "AND e.run_id = (SELECT MAX(id) FROM task_runs r WHERE r.task_id = e.task_id)", _KINDS).fetchone()[0]
    all_rows = conn.execute(f"SELECT COUNT(*) FROM task_events WHERE kind IN ({','.join('?' for _ in _KINDS)})",
                            _KINDS).fetchone()[0]
    assert counted.statements == 1 and counted.rows == newest_rows < all_rows / 5


# --- card notes and PR links ----------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, completed_at INTEGER, result TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT,
                          created_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, started_at INTEGER, ended_at INTEGER,
                        summary TEXT);
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT, body TEXT,
                            created_at INTEGER);
CREATE INDEX idx_runs_task ON task_runs(task_id, started_at);
CREATE INDEX idx_comments_task ON task_comments(task_id, created_at);
"""


def _old_notes(conn, ids):
    """The reader before: every run summary and comment, offered in its index order."""
    ph = ",".join("?" for _ in ids)
    notes, prs = {}, {}

    def offer(task_id, at, text):
        at = at or 0
        line = details._first_line(text)
        if line and at >= notes.get(task_id, (-1, ""))[0]:
            notes[task_id] = (at, line)
        for url in details._PR_URL_RE.findall(str(text or "")):
            if at >= prs.get(task_id, (-1, ""))[0]:
                prs[task_id] = (at, url)

    for r in conn.execute(f"SELECT task_id, COALESCE(ended_at, started_at), summary FROM task_runs "
                          f"WHERE task_id IN ({ph}) AND summary IS NOT NULL", ids):
        offer(*r)
    for r in conn.execute(f"SELECT task_id, created_at, body FROM task_comments WHERE task_id IN ({ph})", ids):
        offer(*r)
    return {t: v[1] for t, v in notes.items()}, {t: v[1] for t, v in prs.items()}


def _details_only(conn, found, monkeypatch):
    for name in ("add_run_machines", "add_run_models", "_add_blocked_upstream"):
        monkeypatch.setattr(details, name, lambda *_a, **_k: None)
    details._add_card_details(conn, found)
    return ({t: c["note"] for t, c in found.items() if "note" in c},
            {t: c["pr_url"] for t, c in found.items() if "pr_url" in c})


TEXTS = ["", " ", "\n\n", "\xa0", " \n", "x", "  fixed it\nmore", "!!", "\t?\n",
         "see https://github.com/o/r/pull/7 now", "https://github.com/o/r/pull/8 and https://github.com/o/r/pull/9",
         "https://github.com/o/r/pull/x", "HTTPS://GITHUB.COM/o/r/pull/3", "done"]


def test_notes_and_pr_links_match_the_old_reader_on_random_boards(monkeypatch):
    for seed in range(60):
        rng = random.Random(seed)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        ids = [f"t{i}" for i in range(6)]
        for t in ids:
            conn.execute("INSERT INTO tasks (id, status) VALUES (?, 'running')", (t,))
            for _ in range(rng.randint(0, 6)):
                start = rng.choice([0, 1, 5, 9, None]) if rng.random() < 0.2 else rng.randint(1, 9)
                conn.execute("INSERT INTO task_runs (task_id, started_at, ended_at, summary) VALUES (?, ?, ?, ?)",
                             (t, start, rng.choice([None, rng.randint(1, 9)]), rng.choice([*TEXTS, None, 0])))
            for _ in range(rng.randint(0, 12)):
                conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'w', ?, ?)",
                             (t, rng.choice([*TEXTS, 0]), rng.choice([rng.randint(1, 9), 0, None])))
        if seed % 10 == 0:  # a non-numeric time: the old reader compares (and may fail) exactly as before
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t0', 'w', 'odd', 'x')")
        found = {t: {"id": t} for t in ids}
        try:
            old = _old_notes(conn, ids)
        except TypeError as exc:
            with pytest.raises(TypeError, match=str(exc)[:20]):
                _details_only(conn, found, monkeypatch)
            continue
        assert _details_only(conn, found, monkeypatch) == old, seed


def test_a_long_comment_thread_hands_out_only_the_rows_that_can_win(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO tasks (id, status) VALUES ('t1', 'running')")
    conn.executemany("INSERT INTO task_runs (task_id, started_at, ended_at, summary) VALUES ('t1', ?, ?, ?)",
                     [(i, i + 1, f"run {i}") for i in range(0, 400, 2)])
    conn.executemany("INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t1', 'w', ?, ?)",
                     [(f"comment {i}", i) for i in range(874)])
    conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES "
                 "('t1', 'w', 'opened https://github.com/o/r/pull/12', 10)")
    old = _old_notes(conn, ["t1"])
    counted = Counting(conn)
    for name in ("add_run_machines", "add_run_models", "_add_blocked_upstream"):
        monkeypatch.setattr(details, name, lambda *_a, **_k: None)
    found = {"t1": {"id": "t1"}}
    details._add_card_details(counted, found, replica=None)  # a caller-owned (absent) shadow: no lookup statement
    assert (found["t1"]["note"], found["t1"]["pr_url"]) == (old[0]["t1"], old[1]["t1"]) == (
        "comment 873", "https://github.com/o/r/pull/12")
    # task_events (none) + one note statement + tasks.result: the newest comment and the PR comment, not 1,075 rows.
    assert counted.statements == 3 and counted.rows == 2


def test_notes_without_a_comments_table_still_read_run_summaries(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA + "DROP TABLE task_comments;")
    conn.execute("INSERT INTO task_runs (task_id, started_at, summary) VALUES ('t1', 5, 'https://github.com/o/r/pull/4')")
    assert _details_only(conn, {"t1": {"id": "t1"}}, monkeypatch) == (
        {"t1": "https://github.com/o/r/pull/4"}, {"t1": "https://github.com/o/r/pull/4"})


# --- external-watch hosts -------------------------------------------------------------------------------------

def _old_hosts(conn, cursor, hosts):
    top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0] or 0
    if top < cursor:
        cursor, hosts = 0, {}
    hosts = dict(hosts)
    for _eid, payload in conn.execute("SELECT id, payload FROM task_events WHERE kind = ? AND id > ?",
                                      (kew.WATCH_EVENT, cursor)):
        rec = kew._json(payload)
        if rec.get("host"):
            at = float(rec.get("observed_at") or 0)
            hosts[str(rec["host"])] = max(hosts.get(str(rec["host"]), 0.0), at)
    return int(top), hosts


@pytest.fixture
def watch_board(tmp_path: Path):
    kew._HOSTS.clear()
    conn = sqlite3.connect(tmp_path / "board.db")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, "
                 "payload TEXT, created_at INTEGER)")
    conn.execute("CREATE INDEX idx_events_kind_task_id ON task_events(kind, task_id, id)")
    yield conn
    kew._HOSTS.clear()


def _watch(conn, payload, kind=None):
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t1', ?, ?, 0)",
                 (kind or kew.WATCH_EVENT, payload))


def test_hosts_match_the_old_incremental_reader_in_one_statement(watch_board):
    rng = random.Random(3)
    cursor, hosts = 0, {}
    for _round in range(8):
        for _ in range(rng.randint(0, 40)):
            _watch(watch_board, rng.choice([
                json.dumps({"host": rng.choice(["hub", "mini", "w1", ""]), "observed_at": rng.randint(0, 99)}),
                json.dumps({"host": "w2"}), json.dumps(["not", "a", "dict"]), "{bad", None, 7, 1.5]),
                kind=rng.choice([None, None, "heartbeat"]))
        cursor, hosts = _old_hosts(watch_board, cursor, hosts)
        counted = Counting(watch_board)
        assert kew.hosts_seen(counted) == hosts
        assert counted.statements == 2 and counted.rows == 2  # database_list, then one row for every new record


def test_hosts_unchanged_cursor_does_not_rescan_watch_history(watch_board):
    for at in range(2000):
        _watch(watch_board, json.dumps({"host": "hub", "observed_at": at}))
    assert kew.hosts_seen(watch_board) == {"hub": 1999.0}
    steps = []
    watch_board.set_progress_handler(lambda: steps.append(1) or 0, 100)
    try:
        assert kew.hosts_seen(watch_board) == {"hub": 1999.0}
    finally:
        watch_board.set_progress_handler(None, 0)
    assert len(steps) < 10, f"unchanged watch cursor scanned history: {len(steps) * 100} VM steps"


def test_hosts_start_over_on_a_smaller_board_behind_the_same_key(watch_board):
    for at in (5, 9):
        _watch(watch_board, json.dumps({"host": "old", "observed_at": at}))
    assert kew.hosts_seen(watch_board) == {"old": 9.0}
    watch_board.execute("DELETE FROM task_events")
    _watch(watch_board, json.dumps({"host": "new", "observed_at": 1}))
    watch_board.execute("UPDATE task_events SET id = 1")
    assert kew.hosts_seen(watch_board) == {"new": 1.0}


def test_hosts_fall_back_to_rows_when_a_payload_is_a_blob(watch_board):
    _watch(watch_board, json.dumps({"host": "hub", "observed_at": 4}))
    _watch(watch_board, sqlite3.Binary(json.dumps({"host": "blob", "observed_at": 6}).encode()))
    assert kew.hosts_seen(watch_board) == _old_hosts(watch_board, 0, {})[1] == {"hub": 4.0, "blob": 6.0}


def test_hosts_keep_the_old_all_or_nothing_failure(watch_board):
    _watch(watch_board, json.dumps({"host": "hub", "observed_at": "soon"}))
    assert kew.hosts_seen(watch_board) == {}


# --- no reader probes a schema one row per column --------------------------------------------------------------

def test_listed_card_readers_probe_each_schema_in_one_row():
    from hermes_cli.kanban_primary_execution import materialize
    from hermes_cli.kanban_replica_execution import read_runs
    from hermes_cli.kanban_run_machine import placement_select
    from hermes_cli.kanban_session_activity import LISTED_STATUSES, _counted_sub_clause
    from hermes_cli.kanban_session_outcome_tasks import select_listed_local_tasks
    from hermes_cli.kanban_session_wait_rows import _local_wait_rows
    from hermes_cli.kanban_watch import not_watch_sql

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, session_id TEXT, "
        "created_at INTEGER, started_at INTEGER, block_kind TEXT, human_request TEXT, current_run_id INTEGER, "
        "last_heartbeat_at INTEGER, ready_since INTEGER, awaiting TEXT, created_on TEXT, runs_on TEXT);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, uid TEXT, status TEXT, started_at INTEGER, "
        "ended_at INTEGER);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, "
        "created_at INTEGER, uid TEXT);"
        "CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, delivery_metadata TEXT);"
        "CREATE TABLE task_chat_links (task_id TEXT, chat_gateway TEXT, chat_profile TEXT, released INTEGER);"
        "INSERT INTO tasks (id, title, status, session_id, current_run_id, created_on) "
        "VALUES ('t1', 'a', 'running', 's1', 1, 'hub'), ('t2', 'b', 'blocked', 's1', NULL, 'hub');"
        "INSERT INTO task_runs VALUES (1, 't1', 'run-1', 'running', 5, NULL);"
        "INSERT INTO task_events (task_id, run_id, kind, created_at, uid) VALUES ('t1', 1, 'heartbeat', 6, 'e1');")
    conn.row_factory = sqlite3.Row
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    found = select_listed_local_tasks(conn, ["s1"], LISTED_STATUSES)
    assert set(found) == {"t1", "t2"}
    assert [tuple(r) for r in materialize(conn, found)] == [("t1", "run-1", '[6,"e1"]', None)]
    assert read_runs(conn, "mini", "default") == {}
    assert "awaiting" in placement_select(conn, names=("awaiting",)) and _counted_sub_clause(conn) != "1"
    assert not_watch_sql(conn) != "1" and set(_local_wait_rows(conn, ["t1", "t2"])[0]) == {"t2"}
    assert statements and not [s for s in statements if "table_info(" in s.lower() and "pragma_table_info" not in s]


# --- blocked-upstream walk -------------------------------------------------------------------------------------

def _old_upstream(conn, found):
    """The walk before: every card re-reads the parents, statuses and authorities it reaches."""
    roots = details._root_parents(conn, None, list(found))
    out = {}
    for task_id, card in found.items():
        if card.get("status") in {"done", "archived", "blocked", "triage"}:
            continue
        blocked, upstream = details._walk(conn, task_id, None, root_parents=roots)
        out[task_id] = {"blocked_upstream": blocked} if blocked is not None else (
            {"upstream": upstream} if upstream is not None else {})
    return out


def test_upstream_walk_matches_the_old_walk_and_reads_shared_ancestors_once(monkeypatch):
    monkeypatch.setattr(details, "_replica_for", lambda _conn: None)
    statuses = ["todo", "ready", "running", "done", "archived", "blocked", "triage", "review"]
    for seed in range(20):
        rng = random.Random(seed)
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, containment_episode TEXT);"
                           "CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY (parent_id, child_id));"
                           "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, uid TEXT, kind TEXT, "
                           "payload TEXT);")
        ids = [f"t{i:02d}" for i in range(50)]
        for i, t in enumerate(ids):
            status = rng.choice(statuses)
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (t, status, f"ep{i}" if status == "triage" else None))
            if status == "triage":
                conn.execute("INSERT INTO task_events (task_id, uid, kind, payload) VALUES (?, ?, 'contained', ?)",
                             (t, f"ep{i}", json.dumps({"policy": {"authority": {"task_id": rng.choice(ids)}}})))
            for parent in rng.sample(ids[i + 1:], min(len(ids) - i - 1, rng.randint(0, 3))):
                conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, t))
        found = {t: {"id": t, "status": rng.choice(statuses)} for t in ids[:30]}
        old = _old_upstream(conn, found)
        old_statements: list[str] = []
        conn.set_trace_callback(old_statements.append)
        _old_upstream(conn, found)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        details._add_blocked_upstream(conn, found)
        conn.set_trace_callback(None)
        got = {t: {k: c[k] for k in ("blocked_upstream", "upstream") if k in c} for t, c in found.items()
               if c.get("status") not in {"done", "archived", "blocked", "triage"}}
        assert got == old, seed
        assert len(statements) <= len(old_statements)


def test_cards_under_one_ancestor_chain_read_it_once(monkeypatch):
    monkeypatch.setattr(details, "_replica_for", lambda _conn: None)
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, containment_episode TEXT);"
                       "CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY (parent_id, child_id));")
    chain = [f"a{i}" for i in range(10)]  # a0 <- a1 <- ... <- a9 (todo), every card a child of a0
    conn.executemany("INSERT INTO tasks VALUES (?, 'todo', NULL)", [(a,) for a in chain])
    conn.executemany("INSERT INTO task_links VALUES (?, ?)", list(zip(chain[1:], chain)))
    found = {f"c{i}": {"id": f"c{i}", "status": "ready"} for i in range(30)}
    conn.executemany("INSERT INTO tasks VALUES (?, 'ready', NULL)", [(c,) for c in found])
    conn.executemany("INSERT INTO task_links VALUES ('a0', ?)", [(c,) for c in found])
    old_statements: list[str] = []
    conn.set_trace_callback(old_statements.append)
    old = _old_upstream(conn, found)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    details._add_blocked_upstream(conn, found)
    assert all(c.get("upstream") is None and "blocked_upstream" not in c for c in found.values()) and old == {
        c: {} for c in found}
    # Old: 30 cards x 10 ancestors x (status + edges) reads. Now each ancestor once, plus the batched roots.
    assert len(old_statements) > 600 and len(statements) <= 2 + 10 * 3
