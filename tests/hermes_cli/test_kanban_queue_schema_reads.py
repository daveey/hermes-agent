"""Fresh admission/containment schema reads must not transfer per-column rows."""
import sqlite3

import pytest

from hermes_cli import kanban_admission_score as score
from hermes_cli import kanban_containment_guard as containment
from hermes_cli.kanban_db_values import _lossy_text
from tests.hermes_cli import test_kanban_score_sources as helpers

sources = helpers.sources


class CountCursor:
    def __init__(self, cursor, owner):
        self.cursor, self.owner = cursor, owner

    def __iter__(self):
        for row in self.cursor:
            self.owner.rows += 1
            yield row

    def fetchone(self):
        row = self.cursor.fetchone()
        self.owner.rows += row is not None
        return row

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.owner.rows += len(rows)
        return rows

    @property
    def description(self):
        return self.cursor.description


class CountConnection:
    def __init__(self, conn):
        self.conn, self.rows = conn, 0

    @property
    def text_factory(self):
        return self.conn.text_factory

    def execute(self, sql, *args):
        return CountCursor(self.conn.execute(sql, *args), self)

    def __getattr__(self, name):
        return getattr(self.conn, name)


@pytest.mark.parametrize('factory', [str, _lossy_text])
@pytest.mark.parametrize('entry', ['admission', 'containment'])
def test_wide_schema_transfer_is_constant_and_refreshed(sources, entry, factory):
    local, _ = sources
    local.text_factory = factory
    helpers.add(local, 'one', 100, 1)
    for i in range(100):
        local.execute(f'ALTER TABLE tasks ADD COLUMN fixture_{i} TEXT')
        local.execute(f'ALTER TABLE task_events ADD COLUMN fixture_{i} TEXT')
    local.execute('ALTER TABLE tasks ADD COLUMN body TEXT')
    local.executemany("INSERT INTO tasks(id,priority,status,body) VALUES (?,1,'done',?)",
                      [(f'closed-{i}', 'x' * 8192) for i in range(2048)])
    observed = CountConnection(local)
    if entry == 'admission':
        assert score._source_snapshot(observed, ['one'])[0] == [('one',1,'ready')]
    else:
        assert containment.pending(observed, 'one') is False
    assert observed.rows < 20, observed.rows
    # Fresh schema/identity changes: no stale column cache or urgency classification.
    local.execute('ALTER TABLE tasks ADD COLUMN idempotency_key TEXT')
    local.execute("UPDATE tasks SET idempotency_key='pickup-stall:one' WHERE id='one'")
    assert score._source_snapshot(local, ['one'])[0] == [('one',900,'ready')]
    local.execute('ALTER TABLE tasks ADD COLUMN containment_episode TEXT')
    local.execute("UPDATE tasks SET containment_episode='fixture-episode' WHERE id='one'")
    assert containment.pending(local, 'one') is True
    local.execute("UPDATE tasks SET containment_episode=NULL WHERE id='one'")
    assert containment.pending(local, 'one') is False


@pytest.mark.parametrize('factory', [bytes, lambda b: b.decode().upper()])
def test_custom_text_factory_matches_literal_pragma(sources, factory):
    from hermes_cli.kanban_queue_read_rows import column_names
    local, _ = sources
    local.text_factory = factory
    expected = {r[1] for r in local.execute('PRAGMA table_info(tasks)')}
    assert column_names(local, 'tasks') == expected
    assert 'id' not in expected


@pytest.mark.parametrize('entry', ['tasks', 'task_events'])
@pytest.mark.parametrize('message', ['no such function: json_group_array', 'database is locked'])
def test_optional_packing_compatibility_keeps_other_errors(sources, entry, message):
    from hermes_cli.kanban_queue_read_rows import column_names
    local, _ = sources

    class Legacy(CountConnection):
        def execute(self, sql, *args):
            if 'json_group_array' in sql:
                raise sqlite3.OperationalError(message)
            return super().execute(sql, *args)

    if message == 'database is locked':
        with pytest.raises(sqlite3.OperationalError, match=message):
            column_names(Legacy(local), entry)
    else:
        assert column_names(Legacy(local), entry) == {r[1] for r in local.execute(f'PRAGMA table_info({entry})')}


def test_equal_but_custom_text_factory_keeps_literal_names(sources):
    from hermes_cli.kanban_queue_read_rows import column_names
    local, _ = sources

    class CustomFactory:
        def __eq__(self, other):
            return other is str

        def __call__(self, value):
            return '<' + value.decode() + '>'

    local.text_factory = CustomFactory()
    expected = {row[1] for row in local.execute('PRAGMA table_info(tasks)')}
    assert column_names(local, 'tasks') == expected


def test_minimal_connection_facade_keeps_original_protocol(sources):
    from hermes_cli.kanban_queue_read_rows import column_names
    local, _ = sources

    class Minimal:
        def execute(self, sql, *args):
            assert 'json_group_array' not in sql, 'unknown facades retain literal protocol'
            return local.execute(sql, *args)

    assert column_names(Minimal(), 'tasks') == {r[1] for r in local.execute('PRAGMA table_info(tasks)')}


def test_pragma_error_is_not_converted_to_empty_columns(sources):
    from hermes_cli.kanban_queue_read_rows import column_names
    local, _ = sources

    class Failed(CountConnection):
        def execute(self, *args):
            raise sqlite3.DatabaseError('schema unavailable')

    with pytest.raises(sqlite3.DatabaseError, match='schema unavailable'):
        column_names(Failed(local), 'tasks')


def test_schema_names_preserve_temp_precedence_and_rollback(sources):
    from hermes_cli.kanban_queue_read_rows import column_names
    local, _ = sources
    assert column_names(local, 'missing_fixture_table') == set()
    local.execute('SAVEPOINT schema_fixture')
    try:
        local.execute('CREATE TEMP TABLE tasks (temp_identity TEXT, quoted_name TEXT)')
        assert column_names(local, 'tasks') == {'temp_identity', 'quoted_name'}
        local.execute('ALTER TABLE tasks ADD COLUMN fresh TEXT')
        assert column_names(local, 'tasks') == {'temp_identity', 'quoted_name', 'fresh'}
    finally:
        local.execute('ROLLBACK TO schema_fixture')
        local.execute('RELEASE schema_fixture')
    assert column_names(local, 'tasks') == {r[1] for r in local.execute('PRAGMA table_info(tasks)')}


@pytest.mark.parametrize('factory', [str, _lossy_text])
def test_packed_replica_donation_shadow_and_fresh_changes(sources, monkeypatch, factory):
    from hermes_cli import kanban_worker_node
    local, remote = sources
    local.text_factory = remote.text_factory = factory
    helpers.add(local, 'candidate', 1000)
    helpers.add(remote, 'child', 1000, 900)
    remote.execute("INSERT INTO task_links VALUES ('candidate','child')")

    class Reader(CountConnection):
        def __init__(self, conn):
            super().__init__(conn)
            self.packed_reads = 0

        def execute(self, sql, *args):
            self.packed_reads += 'json_group_array' in sql
            return super().execute(sql, *args)

        def close(self):
            pass  # fixture owns both real connections

    primary, replica = Reader(local), Reader(remote)
    monkeypatch.setattr(kanban_worker_node, 'open_replica', lambda conn: replica)
    monkeypatch.setattr(score, '_apply_turn_floor', lambda conn, tasks, now_s=None: frozenset())

    def parts():
        return score.rank(primary, [{'id': 'candidate'}], now_ms=2_000_000, weights={})[0][1]

    first = parts()
    assert (first.effective_priority, first.descendants) == (900, 1)
    helpers.add(local, 'child', 1000, 900)
    local.execute("UPDATE tasks SET status='done' WHERE id='child'")
    closed = parts()
    assert (closed.effective_priority, closed.descendants) == (0, 0)
    local.execute("DELETE FROM tasks WHERE id='child'")
    remote.execute("UPDATE tasks SET priority=700 WHERE id='child'")
    fresh = parts()
    assert (fresh.effective_priority, fresh.descendants) == (700, 1)
    assert primary.packed_reads >= 3 and replica.packed_reads >= 3

