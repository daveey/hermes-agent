"""Observer opens must not acknowledge an unpinned board generation."""
import sqlite3

import pytest

from hermes_cli.kanban_db_connect import _dispatch_tick_lock
from hermes_cli.kanban_dispatch_owner import connect_with_identity, connection_db_path
from tests.hermes_cli import test_kanban_failed_result_generation as fixtures

gen = fixtures.gen


def test_generation_only_open_refuses_contended_native_generation(gen):
    key = gen.path.with_name(gen.path.name + '.generation')
    with _dispatch_tick_lock(key, create_parent=False) as held:
        assert held
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            conn = connect_with_identity(gen.path, existing_only=True,
                                         generation_only=True, timeout=0.05)
            conn.close()
    # Releasing the lock permits a genuinely identity-pinned connection.
    conn = connect_with_identity(gen.path, existing_only=True,
                                 generation_only=True, timeout=0.5)
    try:
        assert connection_db_path(conn) == gen.path.resolve()
    finally:
        conn.close()
