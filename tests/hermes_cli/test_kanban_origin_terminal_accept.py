"""An obligation accepted when its owner task is already terminal releases on accept.

[daveey] The accept INSERT used to omit ``released`` (default 0). An obligation accepted after its card was
already done+verified never got a newer replica version, so ``refresh_observations`` never advanced it and the
origin session counted a finished card as unfinished forever (session 20261026_170127_f07c32).
"""
import sqlite3

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_ledger as ledger
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_ledger import world as world, accept  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_evidence import create_bound, publish_observed


def _project(path, replica):
    return ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]


def _released(path):
    with sqlite3.connect(path) as db:
        return db.execute('SELECT released FROM origin_obligations').fetchone()[0]


def test_accept_of_terminal_verified_task_releases(world, board):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        assert kb.complete_task(conn, tid, summary='Built and tested')
        publish_observed(conn, replica, binding, version=3)
    accept(world)
    assert _released(path) == 1
    row = _project(path, replica)
    assert row['status'] == 'done'
    assert row['unfinished'] is False
    assert ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]['unfinished'] is False


def test_accept_of_terminal_unverified_task_stays_unknown(world, board):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))  # no completion event = no proof
        publish_observed(conn, replica, binding, version=3)
    accept(world)
    assert _released(path) == 0
    row = _project(path, replica)
    assert row['reported_status'] == 'done'
    assert row['status'] == 'unknown'
    assert row['unfinished'] is True


def test_accept_of_nonterminal_task_is_unchanged(world, board):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
    accept(world)
    assert _released(path) == 0
    row = _project(path, replica)
    assert row['status'] in ('ready', 'todo', 'running')
    assert row['unfinished'] is True


def test_stuck_terminal_verified_row_is_repaired(world, board):
    """A row written by the old INSERT (released=0 at a terminal verified version) heals on the next refresh."""
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        assert kb.complete_task(conn, tid, summary='Built and tested')
        publish_observed(conn, replica, binding, version=3)
    accept(world)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE origin_obligations SET released=0')
    row = _project(path, replica)
    assert row['status'] == 'done' and row['unfinished'] is False
    assert _released(path) == 1
