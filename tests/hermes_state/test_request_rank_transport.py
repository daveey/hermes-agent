"""Request ranking must not hand every ranked ID back through sqlite3."""
from contextlib import contextmanager

import pytest

pytest_plugins = ["tests.hermes_state.test_recency_hot_rows"]

import hermes_state_recency as recency
from tests.hermes_state.test_recency_hot_rows import (
    _exact, _page, _seed, _stepped, _write,
)


@pytest.mark.parametrize('count', [1, 200, 2000])
@pytest.mark.parametrize('full', [False, True])
def test_rank_id_transport_bound(db, monkeypatch, count, full):
    _seed(db, count + 25)
    if full:
        monkeypatch.setattr(recency, '_candidates', lambda *args: None)
    borrow = db._read_ctx
    steps = [0]

    @contextmanager
    def measured():
        with borrow() as conn:
            conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 100) or 0, 100)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)

    monkeypatch.setattr(db, '_read_ctx', measured)
    for change in ('cold', 'raised', 'lowered'):
        if change == 'raised':
            _write(db, "UPDATE sessions SET last_activity_at = 1800000000 WHERE id = 's00000'")
        if change == 'lowered':
            _write(db, "DELETE FROM messages WHERE session_id = 's00001'")
        steps[0] = 0
        with _stepped(db) as log:
            actual = _page(db, limit=count)
        work = steps[0]
        assert actual == _exact(db, monkeypatch, limit=count)
        ranked = [(sql, n) for sql, n in log if 'chain_max AS' in sql]
        assert len(ranked) <= 4, (change, len(ranked))
        assert work <= 3000 * (count + 25) + 30000, (change, work)
        assert ranked and max(n for _, n in ranked) <= 1, (change, [(n, sql[:80]) for sql, n in ranked])
