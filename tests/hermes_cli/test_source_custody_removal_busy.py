"""Real SQLite contention must map to the removal refusal contract."""
import sqlite3
import pytest
from hermes_cli import kanban_source_pin_store as store
from hermes_cli import kanban_source_removal as removal


def test_real_store_busy_becomes_typed_removal_refusal(tmp_path):
    store.setup()
    conn = sqlite3.connect(store.store_path(), timeout=0, isolation_level=None)
    try:
        conn.execute('PRAGMA locking_mode=EXCLUSIVE')
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(removal.RemovalRefused, match='busy'):
            with removal.guard_removal(tmp_path / 'source'):
                pytest.fail('destructive body entered')
    finally:
        conn.execute('ROLLBACK')
        conn.close()
