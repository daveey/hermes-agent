"""Representation costs must not change diagnostic or bookkeeping semantics."""
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


def test_run_overlay_does_not_build_sql_text_per_field(runs):
    statements = []
    runs.set_trace_callback(statements.append)
    assert rh._signatures(runs, ["t"], ["worker_pid"])
    assert not any("quote(" in s.lower() or "group_concat(" in s.lower() for s in statements)


@pytest.mark.parametrize("before,after", [(1, 1.0), (None, 0), ("1", 1),
    (b"a", "a"), ("a\x00b", "a\x00c"), ("a,b;c", "a,b;d")])
def test_run_signature_preserves_storage_type_and_full_value(runs, before, after):
    runs.execute("UPDATE task_runs SET worker_pid=?", (before,))
    first = rh._signatures(runs, ["t"], ["worker_pid"])
    assert first == rh._signatures(runs, ["t"], ["worker_pid"])
    runs.execute("UPDATE task_runs SET worker_pid=?", (after,))
    assert first != rh._signatures(runs, ["t"], ["worker_pid"])


def test_run_signature_empty_order_and_row_factory(runs):
    first = rh._signatures(runs, ["t", "empty"], ["worker_pid"])
    runs.row_factory = sqlite3.Row
    assert first == rh._signatures(runs, ["empty", "t"], ["worker_pid"])
    runs.execute("INSERT INTO task_runs VALUES (2, 't', NULL)")
    assert first != rh._signatures(runs, ["t"], ["worker_pid"])
    assert rh._signatures(runs, [], ["worker_pid"]) == {}


@pytest.mark.parametrize('values', [(1, 1.0, None, 0), ('a\x00b', 'a\x00c'), (-0.0, 0.0, -0.0)])
def test_retained_overlay_matches_fresh_typed_values(runs, values):
    import marshal
    runs.row_factory = sqlite3.Row
    prev = {}
    for value in values:
        runs.execute('UPDATE task_runs SET worker_pid=?', (value,))
        for _ in range(2):
            got = rh.retained_runs(runs, ['t'], prev, frozenset(), br._rows_by_task)
            expected = br._rows_by_task(runs, 'task_runs', ['t'])
            assert marshal.dumps(dict(got['t'][0]), 2) == marshal.dumps(dict(expected['t'][0]), 2)
            prev = {'t': (None, None, None, None, got['t'])}
