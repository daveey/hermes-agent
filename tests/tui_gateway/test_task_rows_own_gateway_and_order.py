"""``_task_rows`` drops this gateway's own rows and keeps the identity query's (planner) order. [daveey]

Pins two #2409 post-merge mutants of ``tui_gateway/kanban_remote_notices._task_rows`` that the remote
notice suites let through: an unfiltered ``coll = 'tasks'`` read (own cards would enter the no-seq
observation path, which does not filter them itself) and identities re-sorted by rowid.
"""
import json
import sqlite3

from tui_gateway import kanban_remote_notices as remote
from tui_gateway import kanban_remote_observations as observations


def _replica(conn, *, seq=True):
    extra = ", seq INTEGER" if seq else ""
    conn.execute(f"CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT{extra})")


def _task(tid, status="done"):
    return json.dumps({"id": tid, "status": status, "completed_at": 1_800_000_000.0})


def test_task_rows_with_me_returns_only_other_gateways_rows():
    conn = sqlite3.connect(":memory:")
    try:
        _replica(conn)
        rows = [("A", "tasks", f"default/default/t_a{i}", _task(f"t_a{i}"), i) for i in range(3)]
        rows += [("B", "tasks", f"default/default/t_b{i}", _task(f"t_b{i}"), 10 + i) for i in range(3)]
        conn.executemany("INSERT INTO replica VALUES (?,?,?,?,?)", rows)
        assert {gw for gw, *_ in remote._task_rows(conn, "A")} == {"B"}
        assert [key for _, key, _, _ in remote._task_rows(conn, "A")] == [f"default/default/t_b{i}" for i in range(3)]
        assert {gw for gw, *_ in remote._task_rows(conn)} == {"A", "B"}  # no ``me``: every gateway
    finally:
        conn.close()


def test_no_seq_observation_snapshot_ignores_this_gateways_own_cards(tmp_path):
    """The legacy (no ``seq``) observation path reads ``_task_rows(conn, gateway)`` and filters nothing itself."""
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta(k TEXT, v TEXT)")
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [("gateway", "macbook"), ("cursor", "5"), ("head", "5")])
        _replica(conn, seq=False)
        conn.executemany("INSERT INTO replica VALUES (?,?,?,?)", [
            ("macbook", "tasks", "default/default/t_own", _task("t_own")),
            ("hub", "tasks", "default/default/t_remote", _task("t_remote")),
            ("macbook", "tasks", "default/default/t_own_blocked", _task("t_own_blocked", "blocked")),
        ])
    ready, cards, signature = observations._snapshot(path, None, {})
    assert ready and signature is None and cards is not None
    assert [(owner, key) for owner, key, _ in cards] == [("hub", "default/default/t_remote")]


def test_task_rows_keeps_the_identity_query_order_not_rowid_order():
    """With the hub's ``(coll, gw, seq)`` index the planner returns rows in index order, not insert order.

    Cold reads (``_task_rows``) and warm reads (``remote_index_projection._read_rows``, one identity query
    with the same WHERE) must list cards in the same order, so ``_task_rows`` keeps the query's order.
    """
    conn = sqlite3.connect(":memory:")
    try:
        _replica(conn)
        conn.execute("CREATE INDEX replica_poll_metadata ON replica(coll, gw, seq)")
        # Inserted (rowid) order: z-gateway first and seqs descending, so index order differs from rowid order.
        rows = [(gw, "tasks", f"default/default/t_{gw}{s}", _task(f"t_{gw}{s}"), s)
                for gw in ("zeta", "alpha", "mid") for s in (30, 20, 10)]
        rows.append(("me", "tasks", "default/default/t_me", _task("t_me"), 1))
        conn.executemany("INSERT INTO replica VALUES (?,?,?,?,?)", rows)
        for me in ("me", None):
            where, args = ("coll = 'tasks' AND gw != ?", (me,)) if me else ("coll = 'tasks'", ())
            planner = conn.execute(f"SELECT gw, key, seq, data FROM replica WHERE {where}", args).fetchall()
            by_rowid = conn.execute(
                f"SELECT gw, key, seq, data FROM replica WHERE {where} ORDER BY rowid", args).fetchall()
            assert planner != by_rowid, "fixture must make planner order differ from rowid order"
            assert remote._task_rows(conn, me) == planner
    finally:
        conn.close()
