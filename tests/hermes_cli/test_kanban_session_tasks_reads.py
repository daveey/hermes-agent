"""What one ``session_tasks`` call reads: each read-only file opened once per call, and the origin-obligation filter
unchanged. [daveey]

On the hub, 12 task-list reads opened ~195 read-only connections (one per card-scope question, most of them hub.db)
and spent most of their CPU there (2026-11-20).
"""
import os
import random
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_sync, kanban_origin, kanban_origin_obligations as obligations
from hermes_cli import kanban_ro_batch as ro_batch
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli.test_kanban_session_activity import _make_board


def _settle(*paths):
    old = time.time() - 60
    for p in paths:
        os.utime(p, (old, old))


# --- one connection per file per session_tasks call -----------------------------------------------------------------


@pytest.fixture
def two_boards_and_a_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "absent.json"))
    hub = sqlite3.connect(state / "hub.db")
    hub.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    hub.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT)")
    hub.execute("INSERT INTO meta VALUES ('gateway', 'hub')")
    hub.commit()
    hub.close()
    _make_board(tmp_path / "kanban.db", [("t_a1", "running", "s1"), ("t_a2", "ready", "s1")])
    _make_board(tmp_path / "kanban" / "boards" / "ops" / "kanban.db", [("t_b1", "todo", "s1")])
    # Every card-scope question reaches hub.db: no memo may answer it, so only the batch can share the open.
    monkeypatch.setattr(kanban_origin, "_NAME_MAX_AGE_S", 0.0)
    monkeypatch.setattr(kanban_origin, "_NAMES_MAX_AGE_S", 0.0)
    monkeypatch.setattr(kanban_origin, "_name_memo", None)
    monkeypatch.setattr(kanban_origin, "_names_memo", None)
    monkeypatch.setattr(kanban_db_sync, "sync_config", dict)
    monkeypatch.setattr(ksa, "_legacy_remote_links", list)
    ksa._reset_cache_for_tests()
    yield state / "hub.db"
    ksa._reset_cache_for_tests()


def _count_opens(monkeypatch):
    real_open, real_borrow = kanban_origin._open_ro, ro_batch.open_ro
    opened, asked = [], []

    def counted_open(path):
        conn = real_open(path)
        opened.append((str(path), conn))
        return conn

    def counted_borrow(path, opener):
        asked.append(str(path))
        return real_borrow(path, opener)

    monkeypatch.setattr(kanban_origin, "_open_ro", counted_open)
    monkeypatch.setattr(ro_batch, "open_ro", counted_borrow)
    return opened, asked


def test_one_session_tasks_call_opens_hub_db_once_and_closes_it(two_boards_and_a_hub, monkeypatch):
    hub_db = str(two_boards_and_a_hub)
    opened, asked = _count_opens(monkeypatch)
    cards = ksa.session_tasks(["s1"])
    assert sorted(c["id"] for c in cards) == ["t_a1", "t_a2", "t_b1"]
    assert asked.count(hub_db) >= 2, "the fixture no longer asks hub.db once per board: the test proves nothing"
    assert [p for p, _ in opened].count(hub_db) == 1
    for _, conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")  # the call's connections do not outlive it
    opened.clear()
    ksa.session_tasks(["s1"])
    assert [p for p, _ in opened].count(hub_db) == 1, "a connection was kept across calls"


def test_session_tasks_rows_are_the_same_with_and_without_sharing(two_boards_and_a_hub, monkeypatch):
    def rows(cards):
        return sorted(({k: v for k, v in c.items() if k != "execution_observations"} for c in cards),
                      key=lambda r: r["key"])

    def states(cards):
        return sorted((c["key"], [(o.get("source"), o.get("state")) for o in c.get("execution_observations") or []])
                      for c in cards)

    shared = ksa.session_tasks(["s1"])
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(ro_batch, "open_ro", lambda path, opener: opener(path))  # every _ro its own connection
    private = ksa.session_tasks(["s1"])
    assert rows(shared) == rows(private)
    assert states(shared) == states(private)


# --- borrow semantics -------------------------------------------------------------------------------------------------


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


def test_a_borrow_behaves_like_a_fresh_connection(db_file):
    with ro_batch.batch():
        first = kanban_origin._ro(db_file)
        first.row_factory = sqlite3.Row
        first.execute("BEGIN")
        assert first.execute("SELECT v FROM t").fetchone()["v"] == 1
        first.close()
        second = kanban_origin._ro(db_file)
        assert object.__getattribute__(second, "_conn") is object.__getattribute__(first, "_conn")
        assert second.row_factory is None
        assert not second.in_transaction
        writer = sqlite3.connect(db_file)
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        writer.close()
        assert second.execute("SELECT count(*) FROM t").fetchone()[0] == 2  # no snapshot pinned by the borrow
        with pytest.raises(sqlite3.OperationalError):
            second.execute("INSERT INTO t VALUES (3)")  # still read-only
        second.close()
        second.close()  # idempotent


def test_a_file_still_borrowed_gets_a_private_connection(db_file):
    with ro_batch.batch():
        outer = kanban_origin._ro(db_file)
        outer.execute("BEGIN")
        outer.execute("SELECT * FROM t").fetchall()
        inner = kanban_origin._ro(db_file)
        assert not isinstance(inner, ro_batch._Borrowed)
        inner.close()
        assert outer.in_transaction, "the nested reader's close touched the outer reader's transaction"
        outer.close()


def test_nested_batches_share_and_the_outer_one_closes(db_file):
    with ro_batch.batch():
        with ro_batch.batch():
            a = kanban_origin._ro(db_file)
            a.close()
        b = kanban_origin._ro(db_file)
        shared = object.__getattribute__(b, "_conn")
        assert shared is object.__getattribute__(a, "_conn")
        b.close()
        shared.execute("SELECT 1")  # the inner block did not close it
    with pytest.raises(sqlite3.ProgrammingError):
        shared.execute("SELECT 1")


def test_another_thread_never_uses_this_threads_connections(db_file):
    import contextvars
    results = {}
    with ro_batch.batch():
        mine = kanban_origin._ro(db_file)
        mine.close()
        ctx = contextvars.copy_context()  # a background refresh started inside the batch

        def other():
            conn = kanban_origin._ro(db_file)
            results["plain"] = not isinstance(conn, ro_batch._Borrowed)
            results["value"] = conn.execute("SELECT v FROM t").fetchone()[0]
            conn.close()
            with ro_batch.batch():  # its own batch, not the inherited one
                a = kanban_origin._ro(db_file)
                a.close()
                b = kanban_origin._ro(db_file)
                results["own_batch_shares"] = object.__getattribute__(a, "_conn") is object.__getattribute__(b, "_conn")
                results["not_mine"] = object.__getattribute__(b, "_conn") is not object.__getattribute__(mine, "_conn")
                b.close()

        t = threading.Thread(target=ctx.run, args=(other,))
        t.start()
        t.join()
    assert results == {"plain": True, "value": 1, "own_batch_shares": True, "not_mine": True}


def test_without_a_batch_every_ro_is_its_own_connection(db_file):
    a, b = kanban_origin._ro(db_file), kanban_origin._ro(db_file)
    assert not isinstance(a, ro_batch._Borrowed) and a is not b
    a.close()
    b.execute("SELECT 1")
    b.close()


# --- origin obligations -----------------------------------------------------------------------------------------------


def _reference(selected, ledger, *, gateway, profile, ids, statuses, placeholder, machine):
    """The filter as it was before the reorder (hermes-agent d912ee96), with the same predicates."""
    out = list(selected)
    seen = {(c.get("session"), c.get("gateway"), c.get("profile"), c.get("board"), c.get("id")) for c in out}
    for card in ledger:
        if card.get("origin_gateway") != gateway or card.get("origin_profile") != profile:
            continue
        fields = ("session", "gateway", "profile", "board", "id")
        if not all(isinstance(card.get(k), str) and card[k].strip() for k in fields):
            continue
        if card["gateway"] == gateway or card.get("released") or not card.get("unfinished"):
            continue
        if ids is not None and card["session"] not in ids and card.get("origin_session") not in ids:
            continue
        if card.get("status") not in statuses and card.get("status") != "unknown":
            continue
        identity = tuple(card[k] for k in fields)
        if identity in seen:
            continue
        if placeholder(card["session"]) or machine(gateway, card["session"], local_gateway=gateway,
                                                   profile=card.get("origin_profile")):
            continue
        seen.add(identity)
        out.append({**card, "session_id": card["session"], "remote": True, "source": "origin-obligation"})
    return out


def test_preserve_origin_obligations_returns_what_the_old_filter_did(monkeypatch):
    from hermes_cli import kanban_db_notify, kanban_placement

    def placeholder(session):
        return session.startswith("ph")

    def machine(gateway, session, *, local_gateway, profile):
        return session.startswith("m")

    monkeypatch.setattr(kanban_placement, "is_placeholder_session", placeholder)
    monkeypatch.setattr(kanban_db_notify, "is_machine_session", machine)
    rnd = random.Random(20261120)
    pick = rnd.choice
    ledger = []
    for i in range(4000):
        card = {"origin_gateway": pick(["me", "me", "other"]), "origin_profile": pick(["default", "default", "p2"]),
                "session": pick(["s1", "s2", "ph1", "m1", "", "  ", None, 7]),
                "gateway": pick(["me", "far", "far2", "", None]), "profile": pick(["default", "x", " "]),
                "board": pick(["default", "ops", ""]), "id": pick([f"t{i % 50}", "", None]),
                "status": pick(["running", "done", "unknown", "blocked", None]),
                "origin_session": pick(["s1", "s3", None])}
        for key, values in (("released", [True, False, 0, 1, None]), ("unfinished", [True, False, None, 1])):
            if rnd.random() < 0.8:
                card[key] = pick(values)
        ledger.append(card)
    selected = [{"session": "s1", "gateway": "far", "profile": "default", "board": "default", "id": "t1"}]
    for ids in (None, ["s1"], ["s2", "s3"], []):
        for statuses in (("running", "blocked"), ("done",)):
            kw = dict(gateway="me", profile="default", ids=ids, statuses=statuses)
            got = obligations.preserve_origin_obligations(selected, ledger, **kw)
            want = _reference(selected, ledger, placeholder=placeholder, machine=machine, **kw)
            assert got == want
            assert len(want) > len(selected) or ids == [] or statuses == ("done",)


def test_a_shared_connection_closed_behind_the_batch_is_reopened(db_file):
    """Opening another handle to a board can run ``kanban_generation.recover``, which closes this process's idle
    handles on a replaced WAL/SHM generation; the next borrow must not hand out the closed one."""
    with ro_batch.batch():
        first = kanban_origin._ro(db_file)
        stale = object.__getattribute__(first, "_conn")
        first.close()
        stale.close()  # what recover() does to an idle handle
        second = kanban_origin._ro(db_file)
        fresh = object.__getattribute__(second, "_conn")
        assert fresh is not stale
        assert second.execute("SELECT v FROM t").fetchone()[0] == 1
        second.close()
        third = kanban_origin._ro(db_file)
        assert object.__getattribute__(third, "_conn") is fresh  # the reopened one is shared again
        third.close()
    with pytest.raises(sqlite3.ProgrammingError):
        fresh.execute("SELECT 1")


def test_a_closed_batch_is_never_lent_from_a_context_run_later(db_file):
    import contextvars
    with ro_batch.batch():
        ctx = contextvars.copy_context()  # e.g. a callback scheduled inside the batch, run after it ended
    results = {}

    def later():
        conn = kanban_origin._ro(db_file)
        results["plain"] = not isinstance(conn, ro_batch._Borrowed)
        results["value"] = conn.execute("SELECT v FROM t").fetchone()[0]
        conn.close()
        with ro_batch.batch():  # a new block does not join the closed batch
            a = kanban_origin._ro(db_file)
            results["own_batch"] = isinstance(a, ro_batch._Borrowed)
            a.close()

    ctx.run(later)
    assert results == {"plain": True, "value": 1, "own_batch": True}


def test_the_batch_closes_when_session_tasks_raises(two_boards_and_a_hub, monkeypatch):
    from hermes_cli import kanban_session_outcome_tasks as outcome
    hub_db = str(two_boards_and_a_hub)
    opened, _asked = _count_opens(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("remote cards failed")

    monkeypatch.setattr(outcome, "append_remote_listed_cards", boom)
    with pytest.raises(RuntimeError):
        ksa.session_tasks(["s1"])
    assert [p for p, _ in opened].count(hub_db) == 1, "the local boards never asked hub.db: the test proves nothing"
    for _, conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert ro_batch._active.get() is None


def test_the_links_projection_opens_each_file_once(tmp_path, monkeypatch, db_file):
    import hermes_constants
    from hermes_cli import kanban_origin_adapter as adapter, kanban_origin_ledger as ledger, profiles
    (tmp_path / "kanban").mkdir()
    (tmp_path / "kanban" / "origin-ledger.db").touch()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(adapter, "_gateway", lambda _: "origin")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    monkeypatch.setattr(ksa, "_board_db_paths", list)
    monkeypatch.setattr(ksa, "_legacy_remote_links", list)
    reads = []

    def project(*args, **kwargs):  # one read per ledger card, as observe_local_binding does
        for _ in range(5):
            conn = kanban_origin._ro(db_file)
            reads.append(conn.execute("SELECT v FROM t").fetchone()[0])
            conn.close()
        return []

    monkeypatch.setattr(ledger, "project", project)
    opened, asked = _count_opens(monkeypatch)
    ksa._reset_cache_for_tests()
    ksa._project_remote_links()
    assert reads == [1] * 5
    assert asked.count(str(db_file)) == 5
    assert [p for p, _ in opened].count(str(db_file)) == 1
    for _, conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
