"""Sidebar ranking reads test cheap ``sessions`` columns before the model_config JSON lineage
predicates, and the pinned back-fill is index-driven (MacBook idle-CPU: a changed sidebar key
rebuilt three slices that each paid ``json_valid`` + ``json_extract`` on every row, plus a full
``sessions`` scan to find the handful of pins). [daveey]
"""

import re
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_sessions import (
    _cwd_prefix_clause, _session_filter_where, _source_match_clause, _workspace_key_clause,
)

_PLAIN = {
    "source": "s.source IN (",
    "session_key": "s.session_key = ?",
    "exclude_sources": "s.source NOT IN (",
    "cwd_prefix": "s.cwd = ?",
    "workspace_key": "s.git_repo_root = ?",
    "min_message_count": "s.message_count >= ?",
}


def _fixture(path):
    """Most rows sit in an excluded source; every row carries a model_config object."""
    with SessionDB(db_path=path) as db:
        assert db._conn is not None
        rows = []
        for i in range(200):
            src = "kanban" if i % 10 < 7 else ("desktop" if i % 2 else "cli")
            cfg = '{"model": "m"}' if i % 5 else '{"_delegate_from": "r0"}'
            rows.append((f"r{i}", src, i + 1, cfg, "r0" if i % 5 == 0 and i else None,
                         i % 4, int(i % 13 == 0), "/w/p", "/w", f"k{i % 3}"))
        db._conn.executemany(
            "INSERT INTO sessions(id,source,started_at,model_config,parent_session_id,message_count,"
            "archived,cwd,git_repo_root,session_key) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        db._conn.commit()


@pytest.mark.parametrize("include_subagents", [False, True], ids=["hide_subagents", "show_subagents"])
def test_plain_column_clauses_precede_json_predicates(tmp_path, include_subagents):
    path = tmp_path / "state.db"
    _fixture(path)
    where, params = _session_filter_where(
        exclude_children=True, include_subagents=include_subagents, sources=["desktop", "cli"], session_key="k1",
        exclude_sources=["kanban"], cwd_prefix="/w", workspace_key="/w", min_message_count=1)
    sql = " AND ".join(where)
    first_json = sql.index("json_extract")
    for name, marker in _PLAIN.items():
        assert marker in sql, name
        assert sql.index(marker) < first_json, f"{name} is tested after the JSON lineage predicates"
    assert sql.index("s.archived = 0") < first_json
    # Clause-level too: every clause before the first JSON one is plain, every one after is lineage.
    json_at = [i for i, clause in enumerate(where) if "json_extract" in clause]
    assert json_at and json_at == list(range(json_at[0], len(where)))
    assert all("?" not in where[i] for i in json_at)  # lineage clauses bind nothing: params unmoved
    expected = [*_source_match_clause(["desktop", "cli"])[1], "k1", "kanban",
                *_cwd_prefix_clause("/w")[1], *_workspace_key_clause("/w")[1], 1]
    assert params == expected
    with sqlite3.connect(path) as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM sessions s WHERE {sql}", params).fetchone()[0]
    assert count > 0


def _pinned_plan(db):
    import hermes_state_page_pack as page_pack
    seen = []
    read_chunks = page_pack.read_chunks

    def spy(conn, sql, params=()):
        if "_pin_id" in sql:
            seen.append((sql, list(params)))
        return read_chunks(conn, sql, params)

    page_pack.read_chunks = spy
    try:
        db.list_sessions_rich(source=None, exclude_sources=["kanban"], limit=5, min_message_count=1,
                              order_by_last_active=True, compact_rows=True, include_pinned=True)
    finally:
        page_pack.read_chunks = read_chunks
    assert len(seen) == 1, seen
    sql, params = seen[0]
    with db._read_ctx() as conn:
        return [row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()]


def _assert_no_sessions_scan(plan):
    assert not [d for d in plan if re.match(r"SCAN (sessions|s)\b", d)], plan


def test_pinned_backfill_is_index_driven_on_fresh_store(tmp_path):
    path = tmp_path / "state.db"
    _fixture(path)
    with SessionDB(db_path=path) as db:
        db._conn.execute("UPDATE sessions SET pinned = 1 WHERE id IN ('r1', 'r3')")
        db._conn.commit()
        _assert_no_sessions_scan(_pinned_plan(db))


def test_pinned_backfill_index_installed_on_existing_store(tmp_path):
    path = tmp_path / "state.db"
    _fixture(path)
    # A store written by the parent schema: everything but the new index.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_pinned")
        conn.execute("UPDATE sessions SET pinned = 1 WHERE id IN ('r1', 'r3')")
    with SessionDB(db_path=path) as db:
        names = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "idx_sessions_pinned" in names
        _assert_no_sessions_scan(_pinned_plan(db))
