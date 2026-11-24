"""Typed intent binds its home epoch at reservation, never at replay. [daveey]"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from hermes_cli import kanban_actionability_transport as act
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failure_release as release
from hermes_cli import kanban_retain_transport as retain
from tests.hermes_cli import test_kanban_actionability_transport as action_fixture
from tests.hermes_cli import test_kanban_failure_transport as failure_fixture
from tests.hermes_cli import test_kanban_retain_transport as retain_fixture

live = failure_fixture.live


def mirror(conn, task, nonce, pending=0):
    with kbc.write_txn(conn):
        conn.execute('INSERT INTO kanban_home_generations1(task_id,nonce,pending) VALUES (?,?,?) '
                     'ON CONFLICT(task_id) DO UPDATE SET nonce=excluded.nonce,pending=excluded.pending',
                     (task, nonce, pending))


def fixture(conn, kind):
    if kind == 'actionability':
        task, payload = action_fixture.contract(conn)
        return task, lambda: act.reserve(conn, task, payload)
    task, rid = failure_fixture._running(conn)
    if kind == 'retain':
        run = sync.run_uid(conn, rid)
        assert run is not None
        return task, lambda: retain.reserve(conn, task, run, retain_fixture.PAYLOAD, None)
    return task, lambda: release.queue(conn, task, rid, outcome='crashed', error='fixture')


def outbound(conn, kind):
    row = sync.outbox(conn)[0]
    if kind == 'actionability':
        return act.outbound(conn, row)
    if kind == 'retain':
        return retain.outbound(conn, row)
    from hermes_cli import kanban_sync_envelope as envelopes
    from hermes_cli import kanban_sync_wire as wire
    with kbc.write_txn(conn):
        return envelopes.outbound(conn, row, lambda: wire.translate(row, '100-0-fixture'))


@pytest.mark.parametrize('kind', ['retain', 'actionability', 'release'])
@pytest.mark.parametrize('captured', [False, True])
def test_reservation_epoch_survives_new_mirror_and_actual_outbound(live, kind, captured):
    task, reserve = fixture(live, kind)
    if captured:
        mirror(live, task, 'committed-nonce-0001')
    with kbc.write_txn(live):
        owned = reserve()
    first = outbound(live, kind)
    expected = {task: 'committed-nonce-0001'} if captured else None
    assert first.get('home_generations') == expected
    if kind != 'release':
        assert isinstance(owned, dict)
        assert owned['request_sha256'] == hashlib.sha256(owned['request_json'].encode()).hexdigest()
        assert 'home_generations' not in json.loads(owned['request_json'])
    mirror(live, task, 'committed-nonce-0002')
    with kbc.write_txn(live):
        assert reserve() == owned
    assert outbound(live, kind) == first
    mirror(live, task, 'committed-nonce-0002', pending=1)
    with kbc.write_txn(live):
        assert reserve() == owned  # An existing command is not fresh pending intent.
    if kind != 'release':
        with kbc.write_txn(live):
            live.execute('DELETE FROM kanban_sync_outbox')
        with kbc.write_txn(live):
            assert reserve() == owned  # ACK never discards the captured epoch.


@pytest.mark.parametrize('kind', ['retain', 'actionability', 'release'])
def test_pending_transfer_refuses_new_command_without_custody_effects(live, kind):
    task, reserve = fixture(live, kind)
    mirror(live, task, 'committed-nonce-0001', pending=1)
    with pytest.raises(ValueError, match='home transfer pending'):
        with kbc.write_txn(live):
            reserve()
    assert not sync.outbox(live)


@pytest.mark.parametrize('kind', ['retain', 'actionability'])
def test_command_sidecar_is_immutable_and_survives_transport_removal(live, kind):
    task, reserve = fixture(live, kind)
    mirror(live, task, 'committed-nonce-0001')
    with kbc.write_txn(live):
        owned = reserve()
    assert isinstance(owned, dict)
    table = 'kanban_failure_commands' if kind == 'retain' else 'kanban_actionability_commands'
    with pytest.raises(sqlite3.IntegrityError, match='captured command home generation is immutable'):
        with kbc.write_txn(live):
            live.execute(f'UPDATE {table} SET home_generations=NULL')
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_sync_outbox')
    mirror(live, task, 'committed-nonce-0002')
    with kbc.write_txn(live):
        assert reserve() == owned


def test_protect_captures_separate_target_epoch_without_changing_semantic_bytes(live):
    from hermes_cli import kanban_protect_transport as protect
    task, _ = action_fixture.contract(live)
    args = {'source_event_uid': 'source-event', 'source_run_uid': 'source-run', 'expected_status': 'ready',
            'expected_current_run_uid': None, 'expected_updated_at': 1, 'expected_episode': None,
            'requested_owner': 'actor-fixture', 'next_step': 'Resolve scope', 'recovery_trigger': 'Scoped decision',
            'escalate_at': 1000, 'evidence': ['isolated capture fixture']}
    payload = {'action': 'protect_stopped', 'board': 'default', 'home': 'fixture', 'request_uid': 'typed-protect-request',
               'target_task_id': task, 'caller': {'task_id': 'actor-fixture', 'run_uid': 'actor-run'}, 'args': args}
    mirror(live, task, 'committed-nonce-0001')
    with kbc.write_txn(live):
        owned = protect.reserve(live, 'default', payload, 'request-event')
    assert json.loads(owned['home_generations']) == {task: 'committed-nonce-0001'}
    assert json.loads(owned['request_json']) == protect.envelope(payload, 'request-event')
    mirror(live, task, 'committed-nonce-0002', pending=1)
    with kbc.write_txn(live):
        assert protect.reserve(live, 'default', payload, 'request-event') == owned


@pytest.mark.parametrize('kind', ['retain', 'actionability', 'release'])
def test_source_marker_alone_blocks_new_typed_capture_before_any_epoch_mirror(live, kind):
    from hermes_cli import kanban_home_transfer as source
    task, reserve = fixture(live, kind)
    with kbc.write_txn(live):
        source._install(live)
        live.execute('INSERT INTO kanban_home_prepared1 VALUES (?,?,?,?,?)',
                     (task, 'fixture', 'hub', 'malformed', 1))
    assert source.frozen(live, task)
    with pytest.raises(ValueError, match='home transfer pending'):
        with kbc.write_txn(live):
            reserve()
