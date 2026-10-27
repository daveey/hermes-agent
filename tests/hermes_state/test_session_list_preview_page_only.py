"""The session-list preview subquery runs for the returned page only, not every admitted session.

SQLite evaluates every result column for each candidate row before ORDER BY ... LIMIT keeps the page,
so a correlated preview column in the paged SELECT read one message per admitted session: ~0.55 s of a
0.67 s sidebar slice on a 7.6k-session / 600k-message store, three slices per sidebar refresh.
"""

import sqlite3

import pytest

import hermes_state_sessions
from hermes_state import SessionDB

N_SESSIONS = 30
LIMIT = 5


@pytest.fixture()
def db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    for i in range(N_SESSIONS):
        sid = f"s{i:02d}"
        db.create_session(sid, "cli")
        db.append_messages_batch(sid, [
            {"role": "user", "content": f"hello from {sid}", "timestamp": 1_000 + i},
            {"role": "assistant", "content": "hi", "timestamp": 1_000 + i + 0.5},
        ])
    db.set_session_pinned("s00", True)
    yield db
    db.close()


def _count_preview_evaluations(db, monkeypatch, call):
    calls = []
    real_preview = hermes_state_sessions._sql_preview_raw

    def counted_preview(session_id_expr="s.id"):
        expr, alias = real_preview(session_id_expr).rsplit(" AS ", 1)
        return f"count_preview({expr}) AS {alias}"

    def count_preview(value):
        calls.append(value)
        return value

    monkeypatch.setattr(hermes_state_sessions, "_sql_preview_raw", counted_preview)
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.create_function("count_preview", 1, count_preview)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    # Pooled readers opened before the patch would not know the UDF: drop them.
    db.close()
    db = SessionDB(db_path=db.db_path)
    try:
        rows = call(db)
    finally:
        db.close()
    return rows, calls


@pytest.mark.parametrize("order_by_last_active", [True, False])
def test_preview_is_computed_for_page_rows_only(db, monkeypatch, order_by_last_active):
    rows, calls = _count_preview_evaluations(db, monkeypatch, lambda d: d.list_sessions_rich(
        limit=LIMIT, order_by_last_active=order_by_last_active, include_pinned=True, compact_rows=True))

    # The newest LIMIT sessions plus the back-filled pin (s00 is the oldest, so off the page).
    assert [r["id"] for r in rows] == [f"s{i:02d}" for i in range(N_SESSIONS - 1, N_SESSIONS - 1 - LIMIT, -1)] + ["s00"]
    assert all(r["preview"] == f"hello from {r['id']}" for r in rows)
    # One evaluation per returned row (page + pin back-fill), never one per admitted session.
    assert len(calls) == len(rows), f"preview evaluated {len(calls)}x for {len(rows)} rows"


def test_full_rows_keep_preview_and_order(db):
    rows = db.list_sessions_rich(limit=LIMIT, offset=2, order_by_last_active=True, compact_rows=False)

    assert [r["id"] for r in rows] == [f"s{i:02d}" for i in range(N_SESSIONS - 3, N_SESSIONS - 3 - LIMIT, -1)]
    assert all(r["preview"] == f"hello from {r['id']}" for r in rows)
    assert all("_effective_last_active" not in r for r in rows)
