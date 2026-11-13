"""A chat-links reconcile pass and ``creator_chats.backfill`` classify sessions in batches, not per row. [daveey]

Hub, 2026-11-13 (t_01a1d2cc49634a78): the sync loop's rounds slowed from ~1 min to 15-28 min. A 60 s profile of
the sync thread (``py-spy record --idle``) put 65% of its wall time in ``_local_session_sources``, 60% of it from
``_owner_written_releases``' per-row ``is_machine_session``. ``_local_links``' created-link loop and
``_worker_shepherd`` and ``backfill``'s ``is_headless_session`` (inside ``write_txn``, so with the board write
lock held) did the same. Each per-row call opened every home's state.db once its 300 s cache entry was missing
or expired. These tests pin the state.db open count per pass, that ``backfill`` reads no provenance inside its
transaction, and byte-identical output against the per-row implementations (kept verbatim as oracles).
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import time
from typing import Any

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_creator_chats as creator_chats
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_session_provenance as provenance
from hermes_cli import kanban_sync_client as ksc
from hermes_state import SessionDB
from tests.tools.test_kanban_worker_created_chats import make_home

GW = "here"
PROFILES = ("research", "ops")  # plus the default home: 3 homes


def _hash(i: int, tag: str = "a") -> str:
    return "local-" + (f"{tag}{i:07x}" * 8)[:64]


DESKTOP = _hash(1, "d")       # interactive in the default store
WORKER = _hash(2, "e")        # kanban in the default store
RCHAT = _hash(3, "f")         # desktop in research, kanban in default: machine wins for a bare lookup
SPLIT = _hash(4, "c")         # desktop in default, cli in research: conflicting, no proof
UNKNOWN = _hash(5, "b")       # no store knows it
MIXED = [DESKTOP, WORKER, RCHAT, SPLIT, UNKNOWN, "20261101_000000_human", "20261101_000000_cron",
         "20261101_000000_rworker", "20261101_000000_nosuch", "cron_job_20261101", "kanban-placement",
         "kanban-placement-x", f"{GW}/default/{DESKTOP}", f"away/default/{DESKTOP}", "a/b"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: (GW, {GW, "away"}))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    stores: dict[Any, tuple] = {home: ((DESKTOP, "desktop"), (WORKER, "kanban"), (RCHAT, "kanban"), (SPLIT, "desktop"),
                     ("20261101_000000_human", "cli"), ("20261101_000000_cron", "cron"))}
    stores[home / "profiles" / "research"] = ((RCHAT, "desktop"), (SPLIT, "cli"), ("20261101_000000_rworker", "kanban"))
    stores[home / "profiles" / "ops"] = ()
    for path, rows in stores.items():
        path.mkdir(parents=True, exist_ok=True)
        db = SessionDB(db_path=path / "state.db")
        for sid, source in rows:
            db.create_session(sid, source=source)
        db.close()
    assert len(provenance._local_homes(None)) == 3
    notify._source_cache.clear()
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    yield home
    notify._source_cache.clear()
    ksa._reset_cache_for_tests()


@pytest.fixture
def opens(monkeypatch):
    """Every state.db open: (path, whether the board connection was inside a transaction then)."""
    seen: list[tuple[str, bool]] = []
    board: dict[str, Any] = {}
    real = sqlite3.connect

    def connect(database, *a, **kw):
        if "state.db" in str(database):
            conn = board.get("conn")
            seen.append((str(database), bool(conn is not None and conn.in_transaction)))
        return real(database, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return seen, board


def _add_task(conn, task_id: str, *, session=None, created_on=GW, payload=None) -> None:
    conn.execute("INSERT INTO tasks (id, title, status, assignee, created_at, session_id) "
                 "VALUES (?, ?, 'ready', 'default', 1, ?)", (task_id, task_id, session))
    if any(c[1] == "created_on" for c in conn.execute("PRAGMA table_info(tasks)")):
        conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (created_on, task_id))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'created', ?, 1)",
                 (task_id, json.dumps(payload or {})))


def _link(conn, task_id, session, *, gateway=GW, profile=None, kind="created", released=0) -> None:
    conn.execute("INSERT OR REPLACE INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, "
                 "kind, released, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                 (links.link_uid(task_id, gateway, profile, session, kind), task_id, gateway, profile, session, kind,
                  released))


def _resolved(conn, task_id) -> None:
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                 "VALUES (?, 'creator_chats_resolved', '{}', 1)", (task_id,))


# --------------------------------------------------------------------------------------------- oracles (base)

def _frozen_local_links(conn: sqlite3.Connection, gateway: str) -> dict[str, dict[str, Any]]:
    """``_local_links`` at 9f4f557b8eb (per-row ``_worker_shepherd`` / ``is_machine_session``), verbatim."""
    from hermes_cli.kanban_db_notify import is_machine_session, machine_sessions, is_headless_session
    from hermes_cli.kanban_session_activity import _counted_sub_clause
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}
    has_links = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone())
    created = [dict(r) for r in conn.execute(
        "SELECT l.* FROM task_chat_links l WHERE l.chat_gateway=? AND l.kind='created' "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=l.task_id "
        "AND e.kind='creator_chats_resolved')", (gateway,))] if has_links else []
    foreign = {r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != '' AND created_on != ?", (gateway,))} \
        if created and any(c[1] == "created_on" for c in conn.execute("PRAGMA table_info(tasks)")) else set()
    sessions = [tuple(r) for r in conn.execute(
        "SELECT id, session_id, created_at FROM tasks WHERE session_id IS NOT NULL AND session_id != ''")]
    try:
        shepherds = [tuple(r) for r in conn.execute(
            f"SELECT s.task_id, s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
            f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)}")]
    except sqlite3.Error:
        shepherds = []
    machine = machine_sessions(gateway, [r[1] for r in sessions]
                               + [r[1] for r in shepherds], local_gateway=gateway)

    def add(task_id: str, session: str, kind: str, created_at: Any) -> None:
        from hermes_cli.kanban_placement import is_placeholder_session
        if is_placeholder_session(session) or is_headless_session(session) or session in machine:
            return
        uid = links.link_uid(task_id, gateway, None, session, kind)
        out[uid] = {"uid": uid, "task_id": task_id, "chat_gateway": gateway, "chat_profile": None,
                    "chat_session": session, "kind": kind, "released": 0, "created_at": int(created_at or now)}

    for row in created:
        if not is_machine_session(gateway, row["chat_session"], profile=row["chat_profile"], local_gateway=gateway):
            out[row["uid"]] = row if row["task_id"] in foreign else {**row, "released": 0}
    for task_id, session, created_at in sessions:
        add(task_id, session, "created", created_at)
    for task_id, session in shepherds:
        if links.link_uid(task_id, gateway, None, session, "created") not in out:
            add(task_id, session, "shepherd", None)
    return out


def _frozen_owner_written_releases(board: str, gateway: str, want: dict) -> dict[str, dict[str, Any]]:
    """``_owner_written_releases`` at 9f4f557b8eb, verbatim (per-row ``is_machine_session``)."""
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_lockguard import connect
    from hermes_cli.kanban_placement import is_placeholder_session
    path = ksc.shadow_path(board)
    if not path.exists():
        return {}
    conn = connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT l.uid, l.task_id, l.chat_gateway, l.chat_profile, l.chat_session, l.kind, l.created_at, "
            "t.status FROM task_chat_links l JOIN tasks t ON t.id = l.task_id "
            "WHERE l.chat_gateway = ? AND l.kind = 'created' AND l.released = 0 AND l.uid IS NOT NULL "
            "AND t.created_on IS NOT NULL AND t.created_on != ?", (gateway, gateway))]
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["uid"] in want or is_placeholder_session(r["chat_session"]):
            continue
        if r.pop("status") not in links.TERMINAL_STATUSES and not is_machine_session(
                gateway, r["chat_session"], local_gateway=gateway, profile=r["chat_profile"]):
            continue
        out[r["uid"]] = {**r, "released": 1, "created_at": int(r["created_at"] or time.time())}
    return out


def _frozen_backfill(conn: sqlite3.Connection) -> dict:
    """``backfill`` at 9f4f557b8eb, verbatim (per-row ``is_headless_session`` inside ``write_txn``)."""
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.kanban_db_notify import is_headless_session
    from hermes_cli.kanban_placement import is_placeholder_session
    rows = conn.execute(
        "SELECT t.id, t.session_id, e.payload FROM tasks t JOIN task_events e ON e.task_id=t.id "
        "AND e.kind='created' WHERE NOT EXISTS (SELECT 1 FROM task_events done WHERE done.task_id=t.id "
        "AND done.kind='creator_chats_resolved')").fetchall()
    attempted, unresolved = [], []
    with creator_chats._ReplicaReader(conn) as replica_reader, write_txn(conn):
        for row in rows:
            if row["session_id"] and not (is_headless_session(row["session_id"])
                                          or is_placeholder_session(row["session_id"])):
                continue
            creator = json.loads(row["payload"] or "{}").get("creator_task_id")
            if not creator:
                continue
            creator_chats.inherit_chats(conn, row["id"], creator, replica_reader=replica_reader)
            attempted.append(row["id"])
            if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='creator_chats_resolved'",
                                (row["id"],)).fetchone():
                unresolved.append(row["id"])
    return {"attempted": attempted, "unresolved": unresolved}


def _dump(value) -> str:
    return json.dumps(value, sort_keys=False, default=repr)


# ------------------------------------------------------------------------------------------------ cost

def _bound(homes: int, sessions: int) -> int:
    return homes * math.ceil(sessions / 500)


def test_local_links_opens_each_home_once_for_500_sessions(home, opens):
    """500 ``local-…`` sessions (300 creators, 200 resolved ``created`` links) x 3 homes: one read per home.
    At 9f4f557b8eb the created-link loop's per-row ``is_machine_session`` alone opened 200 x 3 state.db files."""
    seen, _board = opens
    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            for i in range(300):
                _add_task(conn, f"t_s{i}", session=_hash(1000 + i))
            for i in range(200):
                _add_task(conn, f"t_c{i}")
                _link(conn, f"t_c{i}", _hash(5000 + i), profile=(None, "default", "research")[i % 3])
                _resolved(conn, f"t_c{i}")
        notify._source_cache.clear()
        seen.clear()
        out = links._local_links(conn, GW)
    assert len(seen) <= _bound(3, 500), f"{len(seen)} state.db opens for 500 sessions x 3 homes"
    assert len(out) == 0  # every hash is unknown: a suppressed receipt, never a chat


def test_local_links_stays_bounded_when_the_pass_outlives_the_cache_ttl(home, opens, monkeypatch):
    """A GIL-starved pass (one simulated second per lookup) outlived the 300 s cache, so every later row reopened
    every home (the hub's 15-28 min rounds). Batched verdicts do not depend on how long the loop takes."""
    seen, _board = opens

    class SlowClock:
        t = 1000.0

        @classmethod
        def monotonic(cls):
            cls.t += 1.0
            return cls.t

    with kbc.connect_closing() as conn:
        with kbc.write_txn(conn):
            for i in range(500):
                _add_task(conn, f"t_s{i}", session=_hash(1000 + i))
        notify._source_cache.clear()
        monkeypatch.setattr(provenance, "time", SlowClock)
        seen.clear()
        links._local_links(conn, GW)
    assert len(seen) <= _bound(3, 500), f"{len(seen)} state.db opens"


def _owner_replica(home, rows: list[tuple]) -> None:
    rep = ksc.shadow_path("default")
    rep.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(rep)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, created_on TEXT);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, "
        "chat_session TEXT, kind TEXT, released INTEGER, created_at INTEGER);")
    for task_id, status, created_on, gateway, profile, session in rows:
        conn.execute("INSERT OR IGNORE INTO tasks VALUES (?, ?, ?)", (task_id, status, created_on))
        conn.execute("INSERT INTO task_chat_links VALUES (?, ?, ?, ?, ?, 'created', 0, 1)",
                     (links.link_uid(task_id, gateway, profile, session, "created"), task_id, gateway, profile,
                      session))
    conn.commit()
    conn.close()


def test_owner_written_releases_opens_each_home_once_for_500_rows(home, opens):
    """The dominant cost in the hub profile (60% of the sync thread): 500 open cards' owner-written links."""
    seen, _board = opens
    _owner_replica(home, [(f"t_{i}", "running", "away", GW, (None, "default", "research")[i % 3], _hash(9000 + i))
                          for i in range(500)])
    notify._source_cache.clear()
    seen.clear()
    out = links._owner_written_releases("default", GW, {})
    assert len(seen) <= _bound(3, 500), f"{len(seen)} state.db opens for 500 rows x 3 homes"
    assert len(out) == 500  # unknown local hashes are machine sessions: released on open cards too


# ------------------------------------------------------------------------------------------ same output

def _mixed_board(conn) -> None:
    profiles = (None, "default", "research", "", "Bad Name!")
    with kbc.write_txn(conn):
        for i, session in enumerate(MIXED):
            _add_task(conn, f"t_s{i}", session=session)
            conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) "
                         "VALUES (?, 'tui', ?, 1)", (f"t_s{i}", MIXED[(i + 3) % len(MIXED)]))
            for j, profile in enumerate(profiles):
                tid = f"t_c{i}_{j}"
                _add_task(conn, tid, created_on=("away" if j % 2 else GW))
                _link(conn, tid, session, profile=profile, released=(i + j) % 2)
                _resolved(conn, tid)
            # an unresolved card's created link is not this pass's (no creator_chats_resolved event)
            _add_task(conn, f"t_u{i}")
            _link(conn, f"t_u{i}", session)


def test_local_links_output_is_byte_identical_to_the_per_row_implementation(home, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1794500000.25)
    with kbc.connect_closing() as conn:
        _mixed_board(conn)
        notify._source_cache.clear()
        old = _frozen_local_links(conn, GW)
        notify._source_cache.clear()
        new = links._local_links(conn, GW)
    assert _dump(list(new.items())) == _dump(list(old.items()))
    sessions = {r["chat_session"] for r in new.values()}
    # the corpus exercises keeps and drops of every kind
    assert {DESKTOP, "20261101_000000_human", "20261101_000000_nosuch"} <= sessions
    leaked = {r["chat_session"] for r in new.values() if r["task_id"].startswith("t_s")}
    assert not leaked & {WORKER, UNKNOWN, "kanban-placement-x", "cron_job_20261101"}  # creators and shepherds
    assert {r["kind"] for r in new.values()} == {"created", "shepherd"}


def test_owner_written_releases_output_is_byte_identical(home, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1794500000.25)
    rows = []
    for i, session in enumerate(MIXED):
        for j, profile in enumerate((None, "default", "research", "", "Bad Name!")):
            rows.append((f"t_{i}_{j}", ("running", "done", "blocked", "archived", "todo")[(i + j) % 5],
                         ("away", GW, "")[j % 3], (GW, "away")[(i * j) % 2], profile, session))
    _owner_replica(home, rows)
    want = {links.link_uid("t_0_0", GW, None, MIXED[0], "created"): {}}
    for w in (want, {}):
        notify._source_cache.clear()
        old = _frozen_owner_written_releases("default", GW, w)
        notify._source_cache.clear()
        new = links._owner_written_releases("default", GW, w)
        assert _dump(list(new.items())) == _dump(list(old.items()))
        assert 0 < len(new) < len(rows)


def test_reconcile_and_machine_links_match_per_row_verdicts(home, monkeypatch, tmp_path):
    """The whole pass (placeholder retirement, releases, machine-link receipts) with the batched classifiers swapped
    for per-row ones gives the same counts, the same rows and the same dry run."""
    monkeypatch.setattr(time, "time", lambda: 1794500000.25)
    _owner_replica(home, [(f"t_o{i}", ("running", "done")[i % 2], "away", GW, (None, "research")[i % 2], s)
                          for i, s in enumerate(MIXED)])
    with kbc.connect_closing() as conn:
        _mixed_board(conn)
        with kbc.write_txn(conn):
            for i, session in enumerate(MIXED):  # stale rows the pass retires or releases
                _link(conn, f"t_old{i}", session, kind="shepherd", profile=(None, "research")[i % 2])
                _link(conn, f"t_rel{i}", session, kind="created", released=1)
    board = home / "kanban.db"
    copy = tmp_path / "copy.db"
    shutil.copy(board, copy)

    def per_row_shepherds(sessions, gateway=None, profile=None):
        return {str(s or "") for s in sessions if links._worker_shepherd(s, "x", gateway, profile)}

    def per_row_machine(gateway, rows):
        out: dict = {}
        for profile, session in rows:
            hit = notify.is_machine_session(gateway, session, local_gateway=gateway, profile=profile)
            out.setdefault((type(profile), profile), set()).update({str(session or "")} if hit else set())
        return out

    def run(path):
        notify._source_cache.clear()
        conn = kbc.connect(path)
        try:
            dry = links.machine_links(conn, "default", GW)
            counts = links.reconcile(conn, "default", GW)
            rows = conn.execute("SELECT uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, "
                                "created_at FROM task_chat_links ORDER BY uid").fetchall()
            return _dump([dry, counts, [tuple(r) for r in rows]])
        finally:
            conn.close()

    new = run(board)
    monkeypatch.setattr(links, "_worker_shepherds", per_row_shepherds)
    monkeypatch.setattr(links, "_machine_by_profile", per_row_machine)
    old = run(copy)
    assert new == old
    assert '"retired": 0' not in new and '"released": 0' not in new  # both rules fired


# ------------------------------------------------------------------------------------------------ backfill

def _backfill_board(conn, n_children: int) -> None:
    """Creators owned by an interactive chat or a worker; children awaiting resolution with headless,
    placeholder, interactive, unknown and empty session ids."""
    with kbc.write_txn(conn):
        _add_task(conn, "t_human")
        _link(conn, "t_human", DESKTOP)
        _add_task(conn, "t_worker", payload={"creator_task_id": "t_human"})
        _link(conn, "t_worker", WORKER)
        _add_task(conn, "t_research")
        _link(conn, "t_research", RCHAT, profile="research")
        _add_task(conn, "t_lost")  # only an unknown receipt hash: nothing owns its children
        _link(conn, "t_lost", UNKNOWN)
        kinds = [None, "kanban-placement", DESKTOP, WORKER, UNKNOWN, "20261101_000000_human", RCHAT, SPLIT]
        for i in range(n_children):
            session = kinds[i % len(kinds)] if i < len(kinds) * 2 else _hash(20000 + i)
            creator = ("t_human", "t_worker", "t_research", "t_lost", None)[i % 5]
            _add_task(conn, f"t_k{i}", session=session,
                      payload={"creator_task_id": creator} if creator else {})


def test_backfill_reads_no_provenance_inside_its_write_transaction(home, opens):
    """Hub stacks at 02:33Z/02:37Z: backfill's per-row ``is_headless_session`` ran under ``BEGIN IMMEDIATE`` while the
    dispatcher's pull waited on that lock (no task_events 02:26-02:36)."""
    seen, board = opens
    with kbc.connect_closing() as conn:
        _backfill_board(conn, 500)
        notify._source_cache.clear()
        board["conn"] = conn
        seen.clear()
        out = creator_chats.backfill(conn)
        board.clear()
    assert out["attempted"]
    inside = [p for p, in_txn in seen if in_txn]
    assert not inside, f"{len(inside)} state.db opens inside write_txn"
    # one batch for the session ids and one prefetch of the chat links, each a read per home
    assert len(seen) <= 2 * _bound(3, 500), f"{len(seen)} state.db opens"


def test_backfill_result_and_rows_match_the_per_row_implementation(home, tmp_path):
    with kbc.connect_closing() as conn:
        _backfill_board(conn, 40)
    board = home / "kanban.db"
    copy = tmp_path / "copy.db"
    shutil.copy(board, copy)

    def run(path, fn):
        notify._source_cache.clear()
        conn = kbc.connect(path)
        try:
            result = fn(conn)
            tables = [conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
                      for t in ("task_chat_links", "kanban_notify_subs")]
            events = conn.execute("SELECT task_id, kind, payload FROM task_events ORDER BY id").fetchall()
            return _dump([result, [[tuple(r) for r in t] for t in tables], [tuple(r) for r in events]])
        finally:
            conn.close()

    new = run(board, creator_chats.backfill)
    old = run(copy, _frozen_backfill)
    assert new == old
    assert "creator_chats_resolved" in new and "creator_chats_unresolved" in new
