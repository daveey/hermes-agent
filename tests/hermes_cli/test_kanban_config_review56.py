"""Malformed construction capabilities refuse only their own card before claim."""
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_dispatch import dispatch_once
from tests.hermes_cli import test_kanban_managed_pin_policy as support

board = support.board


@pytest.mark.parametrize('context', ['invalid', '1048576'])
@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_context_type_failure_preclaim(board, lane, context):
    conn, _ = board
    home = Path(os.environ['HERMES_HOME'])
    profile = home / 'profiles' / 'broken'
    profile.mkdir(parents=True)
    source = home / 'profiles' / 'worker' / 'config.yaml'
    profile.joinpath('config.yaml').write_text(source.read_text().replace(
        '  default:', f'  context_length: {context}\n  default:'))
    bad = kb.create_task(conn, title='Bad context type', assignee='broken', priority=100)
    peer = kb.create_task(conn, title='Healthy peer', assignee='worker', priority=1)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (lane, bad))
    spawned = []
    dispatch_once(conn, spawn_fn=lambda task, *a: spawned.append(task.id))
    assert peer in spawned
    assert bad not in spawned
    assert kb.list_runs(conn, bad) == []
    assert kb.get_task(conn, bad).claim_lock is None
    expected = 'triage' if lane == 'review' and context == '1048576' else lane
    assert kb.get_task(conn, bad).status == expected


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_malformed_context_pickup_zero_imports_and_recovery(gws, imports, lane):
    from hermes_cli import kanban_worker_node as wn
    from tests.hermes_cli import test_kanban_worker_node as gateway
    from tests.hermes_cli import test_kanban_unroutable_pickup_review46 as eligibility
    hub, worker = eligibility._setup(gws, support._managed_yaml())
    bad = gateway._hub_card(hub, title='Malformed profile', priority=100, runs_on=eligibility.PIN)
    hub.enter()
    with kb.write_txn(hub.conn):
        hub.conn.execute('UPDATE tasks SET assignee=?, status=? WHERE id=?', ('broken', lane, bad))
    hub.round()
    peer = gateway._hub_card(hub, title='Healthy pickup', priority=1, runs_on=eligibility.PIN)
    broken = worker.home / 'profiles' / 'broken'
    broken.mkdir(parents=True)
    broken.joinpath('config.yaml').write_text('model:\n  context_length: invalid\n' + support._managed_yaml())
    worker.round()
    worker.enter()
    for _ in range(4):
        worker.tick(max_spawn=2)
        worker.round()
    assert imports.count(bad) == 0
    assert imports.count(peer) == 1
    assert len(kb.list_runs(worker.conn, peer)) == 1
    assert kb.list_runs(worker.conn, bad) == []
    assert kb.get_task(hub.conn, bad).status == lane
    broken.joinpath('config.yaml').write_text(support._managed_yaml())
    worker.enter()
    assert wn.pickup(worker.conn, budget=2) == [bad]
    assert imports.count(bad) == 1


from tests.hermes_cli import test_kanban_unroutable_pickup_review46 as pickup_support

svc = pickup_support.svc
gws = pickup_support.gws
imports = pickup_support.imports
