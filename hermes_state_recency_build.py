"""Prepare the derived recency range in short transactions, never during DB open.

Run explicitly after installation, before the natural sessions successor canary:
``hermes --run-module hermes_state_recency_build PATH/TO/state.db``.
Messages stay authoritative. Interrupted preparation resumes its durable cursor;
readers retain the literal fallback until completion commits. No service stop,
update-fence change, or unbounded CREATE INDEX over the messages table is needed.
"""
import argparse
import math
from pathlib import Path
import sqlite3
import time

# Separate table: building its initially empty range index never scans messages.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS message_recency_candidates (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, timestamp
);
CREATE INDEX IF NOT EXISTS idx_message_recency_range
    ON message_recency_candidates(timestamp, session_id);
CREATE TABLE IF NOT EXISTS message_recency_build (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor INTEGER, ready INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS message_recency_insert AFTER INSERT ON messages BEGIN
    INSERT OR REPLACE INTO message_recency_candidates VALUES(new.id, new.session_id, new.timestamp);
END;
CREATE TRIGGER IF NOT EXISTS message_recency_update AFTER UPDATE OF id, session_id, timestamp ON messages BEGIN
    DELETE FROM message_recency_candidates WHERE id=old.id;
    INSERT OR REPLACE INTO message_recency_candidates VALUES(new.id, new.session_id, new.timestamp);
END;
CREATE TRIGGER IF NOT EXISTS message_recency_delete AFTER DELETE ON messages BEGIN
    DELETE FROM message_recency_candidates WHERE id=old.id;
END;
INSERT OR IGNORE INTO message_recency_build VALUES(1, NULL, 0);
"""


def prepare_batch(db, *, batch_size=128, transaction_seconds=0.05):
    """One resumable batch. False = more work (or interrupted), True = published.

    Use the normal owner's guarded writer/rollback path. The callback's progress
    handler caps SQLite VM work under the write lock; a busy writer is never
    displaced. The caller yields at least the elapsed batch time between calls.
    """
    if db.read_only:
        raise ValueError('recency preparation requires an owner, not a read-only browser')
    if batch_size < 1 or not math.isfinite(transaction_seconds) or transaction_seconds <= 0:
        raise ValueError('positive batch size and transaction budget required')

    def write(conn):
        deadline = time.monotonic() + transaction_seconds
        conn.set_progress_handler(lambda: time.monotonic() >= deadline, 16)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_recency_build' AND type='table'").fetchone() is None:
                from hermes_state_schema import SessionSchemaMixin
                # All tables, triggers and cursor commit together. No partial
                # trigger installation can leave an untracked concurrent write.
                SessionSchemaMixin._execute_ddl_script_transactional(conn.cursor(), SCHEMA_SQL)
            cursor, ready = conn.execute('SELECT cursor, ready FROM message_recency_build WHERE singleton=1').fetchone()
            if ready == 1:
                return True
            rows = conn.execute(
                'SELECT id, session_id, timestamp FROM messages WHERE id >= ? ORDER BY id LIMIT ?'
                if cursor is None else
                'SELECT id, session_id, timestamp FROM messages WHERE id > ? ORDER BY id LIMIT ?',
                (-9223372036854775808 if cursor is None else cursor, batch_size)).fetchall()
            conn.executemany('INSERT OR REPLACE INTO message_recency_candidates VALUES(?, ?, ?)', rows)
            if rows:
                conn.execute('UPDATE message_recency_build SET cursor=? WHERE singleton=1', (rows[-1][0],))
            else:
                # Same writer transaction as the final empty range: concurrent
                # inserts/rewrites/deletes are covered by the installed triggers.
                conn.execute('UPDATE message_recency_build SET ready=1 WHERE singleton=1')
            return not rows
        finally:
            conn.set_progress_handler(None, 0)
    try:
        return db._execute_write(write, patience_s=0.1)
    except sqlite3.OperationalError as exc:
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            return False  # the writer rolled back both candidate rows and cursor
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument('--seconds', type=float, default=600.0)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('--seconds must be positive')
    if not Path(args.database).is_file():
        parser.error('database must be an existing state.db file')
    from hermes_state import SessionDB
    db = SessionDB(db_path=args.database)
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            if prepare_batch(db):
                print('recency candidates ready')
                return 0
            # <=50% preparation duty; no change to sessions-capture duty policy.
            time.sleep(max(0.01, time.monotonic() - started))
        print('recency preparation incomplete; cursor retained, reader fallback unchanged')
        return 1
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
