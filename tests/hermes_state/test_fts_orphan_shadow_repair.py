"""#103840: a ``sqlite3 .recover`` restore re-emits FTS5 shadow tables as ordinary tables
but cannot re-emit the ``CREATE VIRTUAL TABLE`` row. The next SessionDB open then failed
in ``_ensure_fts_schema`` with "fts5: error creating shadow table messages_fts_data: table
already exists". Only families whose vtable row is absent may be repaired; a healthy family's
shadows must survive untouched.
"""

import sqlite3

from hermes_state import SessionDB


def _orphan_family(db_path, family: str) -> None:
    """Emulate the ``.recover`` residue for one family: vtable row gone, shadows kept."""
    raw = sqlite3.connect(db_path)
    raw.isolation_level = None
    raw.execute("PRAGMA writable_schema=ON")
    raw.execute(
        "DELETE FROM sqlite_master WHERE name = ? AND sql LIKE 'CREATE VIRTUAL TABLE%'", (family,),
    )
    version = raw.execute("PRAGMA schema_version").fetchone()[0]
    raw.execute(f"PRAGMA schema_version={version + 1}")
    raw.execute("PRAGMA writable_schema=OFF")
    raw.close()


def _fts_master_rows(db_path, prefix: str) -> list:
    raw = sqlite3.connect(db_path)
    try:
        return raw.execute(
            "SELECT rowid, type, name FROM sqlite_master WHERE name LIKE ? ESCAPE '\\' ORDER BY rowid",
            (prefix.replace("_", "\\_") + "%",),
        ).fetchall()
    finally:
        raw.close()


def test_orphaned_base_family_is_repaired_and_healthy_trigram_untouched(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session("s1", source="cli", model="m")
    for i in range(3):
        db.append_message("s1", "user", f"recovered orphan {i}")
    db.close()

    _orphan_family(db_path, "messages_fts")
    trigram_before = _fts_master_rows(db_path, "messages_fts_trigram")
    assert trigram_before, "fixture needs a live trigram family"
    raw = sqlite3.connect(db_path)
    orphan_shadows = raw.execute(
        "SELECT count(*) FROM sqlite_master WHERE name IN ('messages_fts_data', 'messages_fts_config')"
    ).fetchone()[0]
    raw.close()
    assert orphan_shadows == 2, "fixture must leave the base shadows behind"

    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened._fts_enabled is True
        with reopened._lock:
            hits = reopened._conn.execute(
                "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'orphan'"
            ).fetchone()[0]
        assert hits == 3, "recreated index must be rebuilt from the canonical messages table"
    finally:
        reopened.close()
    # Same rowids: the healthy family was neither dropped nor recreated.
    assert _fts_master_rows(db_path, "messages_fts_trigram") == trigram_before


class _SiblingPublishesBetweenReads:
    """Cursor proxy: a concurrent opener publishes ``family`` right after the vtable probe.

    Autocommit gives each SELECT its own snapshot, so a sibling's CREATE VIRTUAL TABLE can
    commit between "no vtable row" and "shadow tables present" (gateways rebooting at once
    over a fresh state.db, the cell5 concurrent-reboot flake).
    """

    def __init__(self, cursor, publish):
        self._cursor, self._publish = cursor, publish

    def execute(self, sql, params=()):
        result = self._cursor.execute(sql, params)
        if self._publish is not None and "LIKE 'CREATE VIRTUAL TABLE%'" in sql:
            rows = result.fetchall()
            publish, self._publish = self._publish, None
            publish()
            return _Rows(rows)
        return result

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def test_sibling_publishing_family_mid_probe_is_not_treated_as_orphan(tmp_path):
    from hermes_state_fts import _drop_orphan_fts_shadow_tables

    db_path = tmp_path / "state.db"
    opener = sqlite3.connect(db_path, isolation_level=None)
    opener.execute("PRAGMA journal_mode=WAL")
    sibling = sqlite3.connect(db_path, isolation_level=None)

    def publish():
        sibling.execute("CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram')")

    try:
        repaired = _drop_orphan_fts_shadow_tables(
            _SiblingPublishesBetweenReads(opener.cursor(), publish), ("messages_fts_trigram",),
        )
        assert repaired == []
        assert not opener.in_transaction
        assert opener.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        shadows = {r[0] for r in opener.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'messages_fts_trigram_%'"
        )}
        assert {"messages_fts_trigram_config", "messages_fts_trigram_data"} <= shadows
    finally:
        sibling.close()
        opener.close()
