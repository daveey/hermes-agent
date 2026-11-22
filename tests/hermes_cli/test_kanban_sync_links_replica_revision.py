"""Replica transport progress is not a changed chat-link input."""
import os

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import test_kanban_sync_steady_links as f

kanban_home = f.kanban_home
ledger = f.ledger
live = f.live
spies = f.spies
scope = f.scope


def test_replica_bookkeeping_does_not_repeat_plan_or_backfill(live, ledger, kanban_home, spies, scope):
    replica = ksc.open_shadow(f.BOARD, f.ME)
    try:
        # Settle bootstrap WAL pages before warming the cache, as in the row controls.
        # Schema growth must not turn cursor writes into a main-file rewrite signal.
        replica.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        settled = f._settled(live, ledger, kanban_home, spies)
        before = rev.read_key(replica, rev.SHADOW_TABLES)
        stamp = ksc.shadow_path(f.BOARD).stat().st_mtime_ns
        for i in range(4):
            with kbc.write_txn(replica):
                replica.execute('UPDATE kanban_sync_state SET cursor=cursor+1, pulled_at=?', (i,))
            assert rev.read_key(replica, rev.SHADOW_TABLES) == before
            assert ksc.shadow_path(f.BOARD).stat().st_mtime_ns == stamp
            f._pass(kanban_home)
        assert spies == {'reconcile': 0, 'backfill': 0}
        assert f._links(live) == settled
    finally:
        replica.close()


def test_replica_uncovered_subscriptions_cannot_be_skipped(live, ledger, kanban_home, spies, scope):
    replica = ksc.open_shadow(f.BOARD, f.ME)
    try:
        f._settled(live, ledger, kanban_home, spies)
        with kbc.write_txn(replica):
            replica.execute("INSERT INTO kanban_notify_subs (task_id,platform,chat_id,created_at) "
                            "VALUES ('t_missing','hub','other/default/s_chat',1)")
        f._pass(kanban_home)
        assert spies['reconcile'] == 1
        f._pass(kanban_home)
        assert spies['reconcile'] == 2
    finally:
        replica.close()


@pytest.mark.parametrize('change', ['heartbeat', 'lease'])
def test_replica_bookkeeping_rows_do_not_repeat_plan(live, ledger, kanban_home, spies, scope, change):
    replica = ksc.open_shadow(f.BOARD, f.ME)
    try:
        with kbc.write_txn(replica):
            replica.execute("INSERT INTO tasks (id,title,status,created_at,created_on) "
                            "VALUES ('t_foreign','foreign','running',1,?)", (f.OTHER,))
        # Settle schema-bootstrap WAL pages before warming the steady-state cache.
        # Otherwise a later heartbeat can cross SQLite's auto-checkpoint boundary
        # and correctly trigger the independent main-file rewrite invalidator.
        replica.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        settled = f._settled(live, ledger, kanban_home, spies)
        for i in range(3):
            before = rev.read_key(replica, rev.SHADOW_TABLES)
            with kbc.write_txn(replica):
                if change == 'heartbeat':
                    replica.execute("INSERT INTO task_events (task_id,kind,payload,created_at) "
                                    "VALUES ('t_foreign','heartbeat','{}',?)", (i,))
                else:
                    replica.execute("UPDATE tasks SET lease_version=?,sync_version=? WHERE id='t_foreign'",
                                    (i + 1, i + 1))
                replica.execute('UPDATE kanban_sync_state SET cursor=cursor+1')
            assert rev.read_key(replica, rev.SHADOW_TABLES) == before
            f._pass(kanban_home)
        assert spies == {'reconcile': 0, 'backfill': 0}
        assert f._links(live) == settled
    finally:
        replica.close()


@pytest.mark.parametrize('change', ['status', 'link', 'created_event', 'parent'])
def test_replica_content_without_cursor_change_rearms(live, ledger, kanban_home, spies, scope, change):
    replica = ksc.open_shadow(f.BOARD, f.ME)
    try:
        with kbc.write_txn(replica):
            replica.execute("INSERT INTO tasks (id,title,status,created_at,created_on) "
                            "VALUES ('t_foreign','foreign','running',1,?)", (f.OTHER,))
            replica.execute("INSERT INTO tasks (id,title,status,created_at,created_on) "
                            "VALUES ('t_parent','parent','running',1,?)", (f.OTHER,))
        f._settled(live, ledger, kanban_home, spies)
        with kbc.write_txn(replica):
            if change == 'status':
                replica.execute("UPDATE tasks SET status='done' WHERE id='t_foreign'")
            elif change == 'link':
                replica.execute("INSERT INTO task_chat_links "
                                "(uid,task_id,chat_gateway,chat_session,kind,released,created_at) "
                                "VALUES ('foreign-link','t_foreign',?,'s_foreign','created',0,1)", (f.ME,))
            elif change == 'created_event':
                replica.execute("INSERT INTO task_events (task_id,kind,payload,created_at) "
                                "VALUES ('t_foreign','created','{}',1)")
            else:
                replica.execute("INSERT INTO task_links (parent_id,child_id) VALUES ('t_parent','t_foreign')")
        f._pass(kanban_home)
        assert spies == {'reconcile': 1, 'backfill': 1}
    finally:
        replica.close()


def test_replica_main_file_rewrite_signal_rearms(live, ledger, kanban_home, spies, scope):
    replica = ksc.open_shadow(f.BOARD, f.ME)
    try:
        f._settled(live, ledger, kanban_home, spies)
        path = ksc.shadow_path(f.BOARD)
        before = rev.read_key(replica, rev.SHADOW_TABLES)
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
        assert path.stat().st_mtime_ns != stamp.st_mtime_ns
        assert rev.read_key(replica, rev.SHADOW_TABLES) == before
        f._pass(kanban_home)
        assert spies == {'reconcile': 1, 'backfill': 1}
    finally:
        replica.close()


def test_replica_missing_coverage_retries(live, ledger, kanban_home, spies, scope):
    replica = ksc.open_shadow(f.BOARD, f.ME)
    try:
        f._settled(live, ledger, kanban_home, spies)
        with kbc.write_txn(replica):
            rev.drop_triggers(replica)
        for _ in range(2):
            f._pass(kanban_home)
        assert spies == {'reconcile': 2, 'backfill': 2}
    finally:
        replica.close()
