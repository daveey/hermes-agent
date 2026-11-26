"""Composed regressions from the independent truthful-state review."""
import json

import pytest

from hermes_cli import kanban_capacity_evidence as cap
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_nonrunning_transport as transport
from hermes_cli import kanban_execution_source as source
from tests.hermes_cli import test_kanban_nonrunning_transport as helpers

svc = helpers.svc
gws = helpers.gws


@pytest.mark.parametrize('code', ['lease_held', 'offline', 'denied'])
@pytest.mark.parametrize('origin', [sync.ORIGIN_BOARD, sync.ORIGIN_LOCAL, None])
def test_board_refusal_origin_survives_serialization(gws, code, origin):
    home, worker, tid = helpers.prepared(gws)
    worker.enter()
    captured = cap.capture(worker.conn)
    detail = sync.ClaimDeferral(code, origin) if origin is not None else code
    result = dispatch.DispatchResult(claim_deferred=[(tid, detail)])
    assert skips.record_tick(worker.conn, result, captured=captured, machine=worker.name) == 1
    worker.round()
    home.round()
    home.enter()
    why = transport.read(home.conn, [tid], board='default', now=captured['observed_at'] + 1)[tid]
    if origin == sync.ORIGIN_BOARD:
        assert why['code'] == code, why
        assert why['freshness'] == 'current', why
        assert why['scope'] == 'observing_dispatcher'
        assert why['observer'] == worker.name
    else:
        assert why['freshness'] == 'unknown', why


@pytest.mark.parametrize('remote_conflict', [False, True])
def test_remote_conflict_survives_local_snapshot_composition(gws, remote_conflict):
    home, worker, tid = helpers.prepared(gws)
    if remote_conflict:
        at = helpers.publish(worker, tid)
        home.round()
        home.enter()
        event = home.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='dispatch_skipped'", (tid,)).fetchone()
        payload = json.loads(event[0])
        payload['detail'] = 'capacity'
        payload['non_running_v1']['observer'] = 'other-worker'
        # Feed-shaped disposable-store reducer control, NOT service auth proof.
        with kb.write_txn(home.conn):
            kb._append_event(home.conn, tid, 'dispatch_skipped', payload)
            home.conn.execute("UPDATE task_events SET origin_gateway='other-worker' WHERE id=(SELECT max(id) FROM task_events)")
        remote = transport.read(home.conn, [tid], board='default', now=at + 1)[tid]
        assert remote['unknown_reason'] == 'dispatcher_evidence_conflict', remote
    else:
        home.enter()
        at = cap.capture(home.conn)['observed_at']
    captured = cap.capture(home.conn, now=at + 1)
    result = dispatch.DispatchResult(claim_deferred=[(tid, 'capacity')])
    assert cap.record(home.conn, result, board='default', machine=home.name, now=at + 1, captured=captured)
    with source.transaction(home.conn):
        sources = source.assemble(home.conn, [tid], board='default', gateway=home.name, profile='default', observed_at=at + 2)
    why = source.raw({'id': tid}, sources[tid])['non_running_reason']
    if remote_conflict:
        assert why['freshness'] == 'unknown', why
        assert why['unknown_reason'] == 'dispatcher_evidence_conflict', why
        assert why['actor'] == 'unknown'
    else:
        assert why['code'] == 'capacity', why
        assert why['freshness'] == 'current', why
