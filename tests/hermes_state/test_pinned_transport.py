"""Pinned hydration is indexed and transports bounded chunks, not wide rows."""
import json

import pytest

pytest_plugins = ["tests.hermes_state.test_recency_hot_rows"]

from tests.hermes_state.test_recency_hot_rows import _page, _seed, _stepped, _write


@pytest.mark.parametrize('pins', [0, 4, 200])
@pytest.mark.parametrize('history', [3000, 10000])
def test_pinned_hydration_handoffs_and_vm(db, pins, history):
    _seed(db, history)
    _write(db, 'UPDATE sessions SET pinned=1, archived=1 WHERE id < ?', (f's{pins:05d}',))
    _page(db, limit=10)  # warm unrelated discovery/schema paths
    with _stepped(db) as log:
        actual = _page(db, limit=10, include_pinned=True)
    queries = [(sql, n) for sql, n in log if '_pin_id' in sql]
    probes = [(sql, n) for sql, n in queries if sql.startswith('SELECT 1 ')]
    assert len(probes) == 1 and probes[0][1] == bool(pins)
    assert sum(n for sql, n in queries if not sql.startswith('SELECT 1 ')) <= (pins + 63) // 64
    assert len(queries) <= 2  # [daveey] Prepare-only LIMIT 0 has no fetch/handoff to log.
    assert len(actual) == 10 + pins
    assert [r['id'] for r in actual[10:]] == [f's{i:05d}' for i in reversed(range(pins))]
    with db._read_ctx() as conn:
        steps = [0]
        conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 100) or 0, 100)
        try:
            for sql, _ in queries:
                conn.execute(sql, (1, json.dumps(sorted(r['id'] for r in actual[:10])))).fetchall()
        finally:
            conn.set_progress_handler(None, 0)
    # Wide compact rows and chunk ordinals add ~1157 VM instructions per pin;
    # the same bound at both history sizes rejects a sessions scan.
    assert steps[0] <= 1500 * pins + 1000, steps
