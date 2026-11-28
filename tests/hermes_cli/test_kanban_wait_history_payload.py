"""ETA capture must not marshal irrelevant checkpoint/reason prose. [daveey]"""
import json
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_wait_history_batch as batch
from tests.hermes_cli import test_kanban_wait_history_batch as helpers


@pytest.mark.parametrize('blob', [False, True])
def test_large_prose_stays_out_of_python_capture(blob, monkeypatch):
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 1)
        prose = 'checkpoint ' * 20_000
        conn.execute("UPDATE task_events SET payload=json_set(payload,'$.reason',?,'$.checkpoint',?) "
                     "WHERE kind IN ('scheduled','continued')", (prose, prose))
        if blob:
            conn.execute('UPDATE task_events SET payload=CAST(payload AS BLOB)')
        sizes = []
        original = json.loads

        def observe(value, *args, **kwargs):
            sizes.append(len(value))
            return original(value, *args, **kwargs)

        monkeypatch.setattr(batch.json, 'loads', observe)
        assert batch.capture(conn, 'ci')['acme/repo0'] == [1200, 900, 600]
        assert max(sizes) < 4096, sizes


@pytest.mark.parametrize('blob', [False, True])
def test_duplicate_awaiting_and_nested_prose_are_unchanged(blob):
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 1)
        # SQLite's original filter reads the FIRST awaiting.kind, while the
        # Python target decoder reads the LAST awaiting object and ref key.
        payload = ('{"reason":"discard me","checkpoint":{"awaiting":"not a target"},'
                   '"awaiting":{"kind":"ci","ref":"first"},'
                   '"awaiting":{"ref":"wrong","ref":"https://github.com/acme/last/actions/runs/1",'
                   '"reason":"nested reason","checkpoint":"nested checkpoint"}}')
        conn.execute("UPDATE task_events SET payload=? WHERE kind='scheduled'", (payload,))
        if blob:
            conn.execute('UPDATE task_events SET payload=CAST(payload AS BLOB)')
        assert batch.capture(conn, 'ci')['acme/last'] == [1200, 900, 600]


def test_sqlite_json5_does_not_become_python_json_history():
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 1)
        payload = '{awaiting:{kind:"ci",ref:"https://github.com/acme/json5/actions/runs/1"}}'
        try:
            conn.execute("SELECT json_extract(?,'$.awaiting.kind')", (payload,)).fetchone()
        except sqlite3.OperationalError:
            pytest.skip('SQLite does not support JSON5')
        conn.execute("UPDATE task_events SET payload=? WHERE kind='scheduled'", (payload,))
        assert batch.capture(conn, 'ci') == {}
