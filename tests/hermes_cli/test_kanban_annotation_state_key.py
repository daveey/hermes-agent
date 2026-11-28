"""The COUNTS state input is canonical compression lineage, not transcript bytes."""
import contextlib
import sqlite3

import pytest

from hermes_cli import kanban_annotation_lineage as lineage
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli.kanban_origin_adapter import _resolve_session
from hermes_state import SessionDB
from tests.hermes_cli import test_kanban_annotation_parts as helpers

world = helpers.world


def test_transcript_only_write_does_not_force_counts(world, monkeypatch):
    # Use a real state DB rather than the old raw-file stand-in. The message is
    # not a continuation candidate, so its text/activity cannot change an alias.
    db = world.state
    monkeypatch.setenv('HERMES_HOME', str(world.plain.parent))
    from hermes_cli import kanban_annotation_disk as disk
    disk.touch_demand(helpers.SCOPE)
    helpers._refresh()
    before = helpers._reads(world)
    assert before[0] > 0, 'the real holder must have read counts before the storm'
    db.append_message('ordinary', 'user', 'not a lineage input')
    helpers._refresh()
    assert helpers._reads(world) == before


@pytest.mark.parametrize('change', [
    "UPDATE sessions SET model_config='{\"_branched_from\":\"root\"}' WHERE id='b'",
    "UPDATE sessions SET model_config='{\"_delegate_from\":\"root\"}' WHERE id='b'",
    "UPDATE sessions SET model_config='{\"_reset_from\":\"root\"}' WHERE id='b'",
    "UPDATE sessions SET source='tool' WHERE id='b'",
    "UPDATE sessions SET ended_at=1700000020 WHERE id='b'",
    "UPDATE sessions SET last_activity_at=1700000020 WHERE id='a'",
    "UPDATE sessions SET started_at=1700000002 WHERE id='a'",
    "UPDATE sessions SET end_reason='normal' WHERE id='root'",
    "DELETE FROM sessions WHERE id='b'",
])
def test_every_canonical_tip_selection_change_moves_key(tmp_path, change):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.create_session('root', 'cli')
    db.end_session('root', 'compression')
    db.create_session('a', 'cli', parent_session_id='root')
    db.create_session('b', 'cli', parent_session_id='root')
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE sessions SET started_at=1700000000, last_activity_at=1700000010')
        conn.execute("UPDATE sessions SET started_at=1700000001 WHERE id='b'")
    assert _resolve_session(path, 'root') == 'b'
    before = lineage.key(path)
    with sqlite3.connect(path) as conn:
        conn.execute(change)
    assert _resolve_session(path, 'root') != 'b'
    assert lineage.key(path) != before


@pytest.mark.parametrize('damage', ['cycle', 'cap'])
def test_unknown_lineage_is_not_certified(tmp_path, monkeypatch, damage):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.create_session('root', 'cli')
    db.end_session('root', 'compression')
    db.create_session('a', 'cli', parent_session_id='root')
    db.close()
    if damage == 'cycle':
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE sessions SET parent_session_id='a' WHERE id='root'")
            conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='a'")
    else:
        monkeypatch.setattr('hermes_state_compression._CHAIN_CAP', 1)
    with pytest.raises(ValueError, match='origin_lineage_invalid'):
        lineage.key(path)


def test_unknown_lineage_schema_is_not_an_empty_certificate(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.create_session('ordinary', 'cli')
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE messages')
    with pytest.raises(sqlite3.Error):
        lineage.key(path)


def test_lineage_inventory_handoffs_are_independent_of_session_count(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_activity as ksa
    from tests.hermes_cli.test_kanban_reader_hot_rows import Counting

    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.create_session('seed', 'cli')
    db.close()
    with sqlite3.connect(path) as conn:
        conn.executemany('INSERT INTO sessions(id, source, started_at) VALUES (?, ?, ?)',
                         [(f'session-{i:05}', 'cli', 1.0) for i in range(4000)])
    original = ksa.cached_ro
    observed = []

    @contextlib.contextmanager
    def counted(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            proxy = Counting(conn)
            observed.append(proxy)
            yield proxy

    monkeypatch.setattr(ksa, 'cached_ro', counted)
    value = lineage.key(path)
    assert value == (('seed', *(f'session-{i:05}' for i in range(4000))), ())
    assert sum(c.rows for c in observed) <= 3


@pytest.mark.parametrize('rows', [
    [], [('z', None), ('a', 'compression'), ('q"\\\n雪', '')],
    [(None, b'legacy'), (b'id', 'compression'), (1.5, -0.0), ('text', float('inf'))],
])
def test_session_transport_preserves_literal_sqlite_rows(rows):
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE sessions(id, end_reason)')
        conn.executemany('INSERT INTO sessions VALUES (?, ?)', rows)
        expected = tuple(conn.execute('SELECT id, end_reason FROM sessions ORDER BY id'))
        actual = lineage._session_rows(conn)
        assert actual == expected
        assert [[type(cell) for cell in row] for row in actual] == [
            [type(cell) for cell in row] for row in expected]
        assert repr(actual) == repr(expected)


def test_unknown_state_is_never_certified(world, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(world.plain.parent / 'absent'))
    # No state store may be turned into a reusable empty lineage certificate.
    forced, now = parts.plan(helpers.SCOPE, clock=False)
    assert forced == parts.ALL and now is None
    parts.done(helpers.SCOPE, now)
    forced, now = parts.plan(helpers.SCOPE, clock=False)
    assert forced == parts.ALL and now is None
