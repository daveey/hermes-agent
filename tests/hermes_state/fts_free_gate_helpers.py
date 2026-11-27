"""Shared real-store fixtures for wholesale FTS free-gate tests."""



import struct

import pytest

import hermes_state_fts_online

import hermes_state_schema_memo



from hermes_state_common import FTS_STALE_KEY, FTS_TRIGRAM_SQL, _FTS_TRIGGERS

from tests.hermes_state.test_fts_online_recovery import _canonical, _drain, _integrity_lines, _meta, _raw, _seed


_TRIGRAM_TRIGGERS = [t for t in _FTS_TRIGGERS if "trigram" in t]


def _break(path, table):
    """The hub's damage shape in *table*'s b-tree (its root names one child twice), without touching FTS state."""
    with _raw(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        page = bytearray(fh.read(page_size))
        assert page[0] == 5, "fixture needs a multi-page index (interior table root)"
        first_cell = struct.unpack(">H", page[12:14])[0]
        page[8:12] = page[first_cell:first_cell + 4]
        fh.seek((root - 1) * page_size)
        fh.write(page)
    assert any("2nd reference" in line for line in _integrity_lines(path)[0])


def _root(path, name):
    with _raw(path) as conn:
        row = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def _assert_never_double_freed(path, before):
    """Canonical rows unchanged and no page reachable twice or both free and in use."""
    assert _canonical(path)[:len(before)] == before
    lines, _master = _integrity_lines(path)
    assert not [line for line in lines if "2nd reference" in line or line.startswith(("Freelist", "Tree"))]


def _recover(db, path, before):
    """The gateway retry tick recovers the detached store online; the backfill and teardown finish."""
    db._fts_stale_retry_after = 0.0
    assert db.retry_deferred_fts_recovery() is True
    _drain(db)
    assert db.search_messages("alphaword")
    db.close()
    _assert_never_double_freed(path, before)


def _assert_gate_fired(db, path, table, root):
    assert db._fts_stale is True and db._fts_oneshot_failed is True
    assert _meta(path, FTS_STALE_KEY) == "1"
    assert _root(path, table) == root, "the damaged tree was dropped or rebuilt"
    assert any("2nd reference" in line for line in _integrity_lines(path)[0]), "the damaged tree was freed"
    with _raw(path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'messages_fts%'"
                                ).fetchone()


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "state.db"
    _seed(path)
    hermes_state_schema_memo.clear()
    return path, _canonical(path)


@pytest.fixture
def plans(monkeypatch):
    """Spy on the whole-file check: one entry per check, whether the watched handle's lock was held."""
    real = hermes_state_fts_online.plan_online_recovery
    watched, calls = {}, []

    def spy(db_path):
        db = watched.get("db")
        calls.append(db._lock.locked() if db is not None else None)
        return real(db_path)

    monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", spy)
    return watched, calls


def _stale_source_view(path, *, claim):
    with _raw(path) as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'messages_fts_src'").fetchone()[0]
        conn.execute("DROP VIEW messages_fts_src")
        conn.execute(sql + " AND 1 = 1" if " WHERE " in sql.upper() else sql + " WHERE 1 = 1")
        if claim:
            conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_high_water', '1')")
            conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_progress', '1')")


def _behind_v30(path):
    with _raw(path) as conn:
        conn.execute("UPDATE schema_version SET version = 29")


def _schema_version(path):
    with _raw(path) as conn:
        return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def _orphan(path, family):
    with _raw(path) as conn:
        for t in _FTS_TRIGGERS:
            if t.startswith(family + "_") and (family == "messages_fts_trigram" or "trigram" not in t):
                conn.execute(f"DROP TRIGGER IF EXISTS {t}")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE name = ? AND sql LIKE 'CREATE VIRTUAL TABLE%'", (family,))
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")


def _drop_triggers(path, names=_FTS_TRIGGERS):
    with _raw(path) as conn:
        for t in names:
            conn.execute(f"DROP TRIGGER IF EXISTS {t}")


def _claim_without_progress(path):
    with _raw(path) as conn:
        conn.execute("INSERT INTO state_meta (key, value) VALUES ('fts_rebuild_high_water', '42') "
                     "ON CONFLICT(key) DO UPDATE SET value = '42'")
        conn.execute("DELETE FROM state_meta WHERE key = 'fts_rebuild_progress'")


def _foreign_trash(path):
    """Trash no plan vouched for (optimize-storage's demotion, CJK retirement): the trigram shadow tables renamed
    to ``fts_v22_trash_*``, a fresh empty trigram index in their place."""
    with _raw(path) as conn:
        _drop_triggers(path, _TRIGRAM_TRIGGERS)
        conn.execute("DROP VIEW IF EXISTS messages_fts_trigram_src")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE name = 'messages_fts_trigram' "
                     "AND sql LIKE 'CREATE VIRTUAL TABLE%'")
        conn.execute("PRAGMA writable_schema=RESET")
        conn.execute("PRAGMA legacy_alter_table=ON")
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                            "AND name LIKE 'messages_fts_trigram_%'")]
        for name in names:
            conn.execute(f"ALTER TABLE {name} RENAME TO fts_v22_trash_{name}")
        conn.executescript(FTS_TRIGRAM_SQL)
    return [f"fts_v22_trash_{n}" for n in names]


def _tables(path, like):
    with _raw(path) as conn:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
                                                 (like,)))


def _share_across_families(path):
    """Point a child pointer of one family's interior root at the other family's first child, so the two trees share
    a page, and keep the first arrangement whose whole-file check names exactly ONE family. Returns
    ``(named, other)``: freeing *other*'s trees would free the page *named* still reaches."""
    import itertools
    import shutil

    with _raw(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        roots = dict(conn.execute("SELECT name, rootpage FROM sqlite_master WHERE rootpage > 0"))
    pristine = path.with_name("pristine.db")
    shutil.copy(path, pristine)

    def page(n):
        with open(path, "rb") as fh:
            fh.seek((n - 1) * page_size)
            return bytearray(fh.read(page_size))

    interior = {n: r for n, r in roots.items() if hermes_state_fts_online.family_of(n) and page(r)[0] == 5}
    for src, dst in itertools.permutations(sorted(interior), 2):
        if hermes_state_fts_online.family_of(src) == hermes_state_fts_online.family_of(dst):
            continue
        shutil.copy(pristine, path)
        a, b = page(interior[src]), page(interior[dst])
        a[8:12] = b[8:12]  # src's right-most child pointer -> dst's right-most child
        with open(path, "r+b") as fh:
            fh.seek((interior[src] - 1) * page_size)
            fh.write(a)
        lines, master = _integrity_lines(path)
        try:
            named = hermes_state_fts_online.plan_from_check(lines, master)["unlink"]
        except hermes_state_fts_online.OnlineRecoveryRefused:
            continue
        if len(named) == 1:
            other = next(f for f in hermes_state_fts_online.FAMILIES if f != named[0])
            return named[0], other
    pytest.skip("no cross-family sharing arrangement that names one family on this SQLite build")


