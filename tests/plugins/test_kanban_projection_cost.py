"""[daveey] Projection costs preserve response semantics, not unread bookkeeping."""
import sqlite3
from types import MappingProxyType

import pytest

from hermes_cli import kanban_diagnostics as kd
from plugins.kanban.dashboard import board_read as br
from plugins.kanban.dashboard import board_run_history as rh


class KeyScanDict(dict):
    def keys(self):
        pytest.fail("mapping fields must not enumerate columns")


def test_mapping_field_lookup_does_not_enumerate_columns():
    for row in (KeyScanDict(status="ready", nullable=None),
                MappingProxyType(KeyScanDict(status="ready", nullable=None))):
        assert kd._task_field(row, "status") == "ready"
        assert kd._task_field(row, "nullable", "default") is None
        assert kd._task_field(row, "absent", "default") == "default"


def test_board_normalizes_diagnostic_task_lookup(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 't' AS id, 'ready' AS status").fetchone()
    seen = []
    def compute(task, *args, **kwargs):
        seen.append(task)
        assert type(task) is dict
        return []
    monkeypatch.setattr(kd, "compute_task_diagnostics", compute)
    br.diagnostics_from_inputs(((row,), {"t": ()}, {"t": ()}, {}, {}))
    assert seen[0] == dict(row)
    conn.close()


@pytest.fixture
def runs():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, worker_pid)")
    conn.execute("INSERT INTO task_runs VALUES (1, 't', 1)")
    yield conn
    conn.close()


def test_run_signatures_seek_candidates_without_scanning_history(runs):
    """[daveey] VM work is independent of ordinary history, not merely result row count."""
    def steps():
        calls = []
        runs.set_progress_handler(lambda: calls.append(1) or 0, 1)
        assert rh._signatures(runs, [1], ['worker_pid']) == {1: (True,)}
        runs.set_progress_handler(None, 0)
        return len(calls)
    before = steps()
    runs.executemany("INSERT INTO task_runs VALUES (?, 't', ?)", [(i, i) for i in range(2, 300)])
    assert steps() <= before + 10


@pytest.mark.parametrize("before,after", [(1, 1.0), (None, 0), ("1", 1),
    (b"a", "a"), ("a\x00b", "a\x00c"), ("a,b;c", "a,b;d")])
def test_run_signature_preserves_only_nullness(runs, before, after):
    runs.execute("UPDATE task_runs SET worker_pid=?", (before,))
    first = rh._signatures(runs, [1], ["worker_pid"])
    runs.execute("UPDATE task_runs SET worker_pid=?", (after,))
    again = rh._signatures(runs, [1], ["worker_pid"])
    assert (first == again) == ((before is None) == (after is None))


def test_run_signature_empty_order_and_row_factory(runs):
    first = rh._signatures(runs, [1, 2], ["worker_pid"])
    runs.row_factory = sqlite3.Row
    assert first == rh._signatures(runs, [2, 1], ["worker_pid"])
    runs.execute("INSERT INTO task_runs VALUES (2, 't', NULL)")
    assert first != rh._signatures(runs, [1, 2], ["worker_pid"])
    assert rh._signatures(runs, [], ["worker_pid"]) == {}


@pytest.mark.parametrize('changed', [None, frozenset({'t'})])
@pytest.mark.parametrize('values', [(1, 1.0, None, 0), ('a\x00b', 'a\x00c'), (-0.0, 0.0, -0.0)])
def test_legacy_and_dirty_runs_still_match_fresh_typed_values(runs, values, changed):
    """[daveey] Only the trusted clean path relaxes the full-row freshness contract."""
    import marshal
    runs.row_factory = sqlite3.Row
    prev = {}
    for value in values:
        runs.execute('UPDATE task_runs SET worker_pid=?', (value,))
        for _ in range(2):
            got = rh.retained_runs(runs, ['t'], prev, changed, br._rows_by_task)
            expected = br._rows_by_task(runs, 'task_runs', ['t'])
            assert marshal.dumps(dict(got['t'][0]), 2) == marshal.dumps(dict(expected['t'][0]), 2)
            prev = {'t': (None, None, None, None, got['t'])}
