"""Runtime custody inventory gate (approved design §8); no source-text completeness pins. [daveey]"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_sync_capture_sites as inventory
from tests.hermes_cli.custody_authorizer import CustodyGuard


@pytest.fixture
def guarded(monkeypatch):
    guard = CustodyGuard()
    monkeypatch.setattr(sqlite3, "connect", guard.factory(sqlite3.dbapi2.connect))
    conn = sqlite3.connect(":memory:")
    conn.executescript('''
        CREATE TABLE kanban_sync_outbox (id INTEGER PRIMARY KEY, x INTEGER);
        CREATE TABLE kanban_sync_parent_envelopes (id INTEGER PRIMARY KEY, x INTEGER);
        INSERT INTO kanban_sync_outbox VALUES (1, 0);
        INSERT INTO kanban_sync_parent_envelopes VALUES (1, 0);
    ''')
    yield guard, conn
    conn.close()


def writer(source, name="unaudited", module="hermes_cli.kanban_db_recover"):
    """Executable controlled mutants, not a scan of production source."""
    namespace = {"__name__": module}
    exec(compile(f"def {name}(c, table):\n" + "\n".join("    " + line for line in source.splitlines()),
                 "<custody-writer-mutant>", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("statement", [
    'c.execute("DELETE FROM %s" % table)',
    'c.execute("DELETE " + "FROM main." + table)',
    'c.execute(f\'DELETE FROM "{table}"\')',
    'c.execute("UPDATE %s AS t SET x=2" % table)',
    'c.execute("UPDATE {} AS t SET x=2".format(table))',
    'q = "DELETE FROM "; q += table; c.execute(q)',
    'c.cursor().execute("DELETE FROM " + table)',
    'c.executemany("UPDATE " + table + " SET x=?", [(2,), (3,)])',
    'c.executescript("DELETE FROM " + table)',
])
@pytest.mark.parametrize("table", sorted({k[3] for k in inventory.SITES}))
def test_unlisted_writer_is_denied_by_resolved_table(guarded, statement, table):
    guard, conn = guarded
    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        writer(statement)(conn, table)
    assert guard.violations[-1][0:2] == ("hermes_cli.kanban_db_recover", "unaudited")
    assert conn.execute(f"SELECT x FROM {table}").fetchall() == [(0,)]


def test_round4_non_sql_pin_cannot_be_traded_for_a_write(guarded):
    guard, conn = guarded
    # Exact round-4 shape: errors='replace' changes to 'ignore' and a dynamic deletion is added
    # in an existing, non-custody-authorized production function. No string counts are consulted.
    probe = writer('''"ignore"  # formerly errors="replace"
for (t,) in c.execute("SELECT name FROM sqlite_master WHERE name = ?", (table,)):
    c.execute("DELETE FROM %s" % t)''', name="spawn_recovery")
    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        probe(conn, "kanban_sync_outbox")
    assert guard.violations[-1][1] == "spawn_recovery"


def test_ordinary_python_and_non_custody_writes_are_unaffected(guarded, tmp_path):
    guard, conn = guarded
    with (tmp_path / "text").open("w", errors="replace") as stream:
        stream.write("delete core-update insert")
    writer('c.execute("CREATE TABLE harmless (x)"); c.execute("INSERT INTO harmless VALUES (1)")')(conn, "harmless")
    assert guard.violations == []


def test_allowed_function_is_anchored_to_exact_module_function_verb_and_table(guarded):
    guard, conn = guarded
    allowed = writer('c.execute("INSERT INTO " + table + " VALUES (2, 1)")', "_salvage_table")
    allowed(conn, "kanban_sync_outbox")
    for name, module, sql in [
        ("prefix_salvage_table", "hermes_cli.kanban_db_recover", "INSERT INTO "),
        ("_salvage_table", "other.kanban_db_recover", "INSERT INTO "),
        ("_salvage_table", "hermes_cli.kanban_db_recover", "DELETE FROM "),
    ]:
        statement = f'c.execute("{sql}" + table' + (' + " VALUES (3, 1)"' if sql.startswith("INSERT") else '') + ')'
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            writer(statement, name, module)(conn, "kanban_sync_outbox")
    assert len(guard.violations) == 3


def test_cached_statement_cannot_borrow_an_allowed_callers_authorization(guarded):
    _, conn = guarded
    sql = 'c.execute("DELETE FROM " + table)'
    writer(sql, "prune_parents", "hermes_cli.kanban_sync_envelope")(conn, "kanban_sync_parent_envelopes")
    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        writer(sql)(conn, "kanban_sync_parent_envelopes")


def test_replacing_authorizer_does_not_remove_guard(guarded):
    _, conn = guarded
    conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
    conn.set_authorizer(None)
    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        writer('c.execute("DELETE FROM " + table)')(conn, "kanban_sync_outbox")


def test_unlisted_trigger_is_denied_even_when_invoked_by_test_fixture(guarded):
    _, conn = guarded
    writer('c.executescript("CREATE TABLE domain (x); CREATE TRIGGER unknown AFTER INSERT ON domain "'
           ' + "BEGIN DELETE FROM " + table + "; END;")')(conn, "kanban_sync_outbox")
    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        conn.execute("INSERT INTO domain VALUES (1)")


def test_independent_memory_databases_cannot_borrow_trigger_identity(guarded):
    _, first = guarded
    trigger = ('c.executescript("CREATE TABLE domain (x); CREATE TRIGGER same AFTER INSERT ON domain "'
               ' + "BEGIN INSERT INTO " + table + " VALUES (2, 1); END;")')
    writer(trigger)(first, "kanban_sync_outbox")
    second = sqlite3.connect(":memory:")
    try:
        second.execute("CREATE TABLE kanban_sync_outbox (id INTEGER PRIMARY KEY, x INTEGER)")
        writer(trigger, "install_triggers", "hermes_cli.kanban_db_sync")(second, "kanban_sync_outbox")
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            first.execute("INSERT INTO domain VALUES (1)")
        second.execute("INSERT INTO domain VALUES (1)")
        assert second.execute("SELECT x FROM kanban_sync_outbox").fetchall() == [(1,)]
    finally:
        second.close()


def test_fixture_trigger_cannot_exempt_a_production_writer(guarded):
    _, conn = guarded
    conn.executescript("CREATE TABLE domain (x); CREATE TRIGGER fixture AFTER INSERT ON domain "
                       "BEGIN DELETE FROM kanban_sync_outbox; END;")
    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        writer('c.execute("INSERT INTO domain VALUES (1)")')(conn, "kanban_sync_outbox")


@pytest.mark.parametrize("kind", ["corrupt", "locked", "non_utf8"])
def test_connect_does_not_read_schema(tmp_path, kind):
    path = tmp_path / "fixture.db"
    raw_connect = sqlite3.dbapi2.connect
    lock = None
    if kind == "corrupt":
        path.write_bytes(b"not a database")
    else:
        lock = raw_connect(path)
        if kind == "locked":
            lock.execute("CREATE TABLE ordinary (x)")
            lock.commit()
            lock.execute("BEGIN EXCLUSIVE")
        else:
            lock.executescript("PRAGMA writable_schema=ON; CREATE TABLE ordinary(x); "
                               "UPDATE sqlite_master SET sql=CAST(x'ff' AS TEXT) WHERE name='ordinary';")
            lock.commit()
    connection = None
    try:
        connection = CustodyGuard().factory(raw_connect)(path, timeout=0)
    finally:
        if connection is not None:
            connection.close()
        if lock is not None:
            lock.close()


def test_guard_does_not_leave_a_reader_that_blocks_checkpoint(tmp_path):
    connect = CustodyGuard().factory(sqlite3.dbapi2.connect)
    conn = connect(tmp_path / "checkpoint.db")
    try:
        conn.execute("PRAGMA journal_mode=WAL").fetchall()
        conn.executescript("CREATE TABLE domain(x); CREATE TRIGGER fixture AFTER INSERT ON domain "
                           "BEGIN SELECT 1; END;")
        conn.execute("INSERT INTO domain VALUES (1)")
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall() == [(0, 0, 0)]
    finally:
        conn.close()


def test_successful_backup_retains_exact_trigger_creator(guarded):
    guard, source = guarded
    writer('c.executescript("CREATE TABLE domain(x); CREATE TRIGGER audited AFTER INSERT ON domain "'
           ' + "BEGIN INSERT INTO " + table + " VALUES (2, 1); END;")',
           "install_triggers", "hermes_cli.kanban_db_sync")(source, "kanban_sync_outbox")
    source.commit()
    target = sqlite3.connect(":memory:")
    try:
        source.backup(target)
        target.execute("INSERT INTO domain VALUES (1)")
        assert target.execute("SELECT x FROM kanban_sync_outbox WHERE id=2").fetchall() == [(1,)]
        assert not guard.violations
    finally:
        target.close()


def test_custom_connection_class_is_stable_across_opens():
    class Custom(sqlite3.Connection):
        pass

    connect = CustodyGuard().factory(sqlite3.dbapi2.connect)
    first = connect(":memory:", factory=Custom)
    second = connect(":memory:", factory=Custom)
    try:
        assert type(first) is type(second)
        assert isinstance(first, Custom)
    finally:
        first.close()
        second.close()


def test_connect_keeps_existing_explicit_unwrapped_probe_interface():
    def wrapper(*args, **kwargs):
        return sqlite3.dbapi2.connect(*args, **kwargs)

    wrapper.__wrapped__ = sqlite3.dbapi2.connect
    connect = CustodyGuard().factory(wrapper)
    assert connect.__wrapped__ is wrapper.__wrapped__


def test_dispositions_and_original_candidates_remain_bounded():
    assert set(inventory.SITES.values()) <= inventory.DISPOSITIONS
    for (_, _, verb, _), disposition in inventory.SITES.items():
        if disposition in (inventory.ORIGINAL_CANDIDATE, inventory.RECOVERY_AMBIGUOUS, inventory.SYNTHETIC_UNKNOWN):
            assert verb == "INSERT"
