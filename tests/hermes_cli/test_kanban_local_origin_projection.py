"""Local owner evidence, not stale replicas, governs same-owner obligations."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_ledger as ledger
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414
from tests.hermes_cli.test_kanban_origin_ledger import world as world, publish, accept  # noqa: PLC0414
from tests.hermes_cli.test_kanban_origin_evidence import create_bound


def setup_local(world, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    _, replica, binding = world
    binding['origin']['gateway'] = 'owner'
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
    publish(replica, binding)
    accept(world, gateway='owner')
    return tid


def projection(world):
    return ledger.project(world[0], replica=None, gateway='owner', profile='default')[0]


def test_local_completion_and_reopen_override_replica_without_mutating_receipt(world, board, monkeypatch):
    tid = setup_local(world, monkeypatch)
    before = ledger.receipt(world[0], 'request-0001')
    assert projection(world)['status'] == 'ready'
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary='Built and tested')
    row = projection(world)
    assert row['status'] == 'done' and not row['unfinished'] and not row['stale']
    assert row['observation_version'] == 3
    assert row['origin_session'] == 's_old' and row['session'] == 's_current'
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input',completed_at=NULL WHERE id=?", (tid,))
    row = projection(world)
    assert row['status'] == 'blocked' and row['unfinished']
    assert row['block_kind'] == 'needs_input'
    assert ledger.receipt(world[0], 'request-0001') == before
    with sqlite3.connect(world[0]) as conn:
        assert conn.execute('SELECT observation_version,released FROM origin_obligations').fetchone() == (3, 0)


@pytest.mark.parametrize('fault', ['raw_done', 'archive', 'missing_marker', 'wrong_marker', 'missing_board'])
def test_local_unverified_terminal_or_binding_never_releases(world, board, monkeypatch, fault):
    tid = setup_local(world, monkeypatch)
    with kbc.connect_closing() as conn:
        if fault == 'raw_done':
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        elif fault == 'archive':
            assert kb.archive_task(conn, tid)
        else:
            assert kb.complete_task(conn, tid, summary='Built and tested')
            if fault == 'missing_marker':
                conn.execute('DELETE FROM origin_creation_intents')
            elif fault == 'wrong_marker':
                marker = json.loads(conn.execute('SELECT binding_json FROM origin_creation_intents').fetchone()[0])
                marker['origin']['session'] = 'unrelated'
                conn.execute('UPDATE origin_creation_intents SET binding_json=?', (json.dumps(marker),))
    if fault == 'missing_board':
        kb.kanban_db_path('default').rename(board / 'unavailable.db')
    row = projection(world)
    assert row['status'] == 'unknown' and row['unfinished']


def test_unreadable_local_owner_does_not_trust_fresh_terminal_replica(world, board, monkeypatch):
    from tests.hermes_cli.test_kanban_origin_evidence import publish_observed
    tid = setup_local(world, monkeypatch)
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary='Built and tested')
        publish_observed(conn, world[1], world[2], version=4, now=1000)
    kb.kanban_db_path('default').rename(board / 'unavailable.db')
    row = ledger.project(world[0], replica=world[1], gateway='owner', profile='default', now=1001)[0]
    assert row['status'] == 'unknown' and row['unfinished'] and row['stale']


@pytest.mark.parametrize('fault', ['active_profile', 'board_override'])
def test_ambiguous_local_board_route_cannot_release(world, board, monkeypatch, fault):
    from hermes_cli import profiles
    tid = setup_local(world, monkeypatch)
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, tid, summary='Built and tested')
    if fault == 'active_profile':
        monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'other')
    else:
        monkeypatch.setenv('HERMES_KANBAN_DB', str(board / 'other.db'))
        monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    row = projection(world)
    assert row['status'] == 'unknown' and row['unfinished']
