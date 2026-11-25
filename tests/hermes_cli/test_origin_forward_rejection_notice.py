"""Late forwarding rejection must land a real owning-chat notice before it is final."""
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from tests.hermes_cli import test_origin_forward_adapter as helpers

world = helpers.world
envelope = helpers.envelope


def notice_rows(world):
    with sqlite3.connect(world[0] / 'state.db') as db:
        return db.execute("SELECT session_id, content, display_metadata FROM messages "
                          "WHERE display_kind='kanban_notice'").fetchall()


def test_missing_binding_is_not_silent_and_replay_never_duplicates(world, monkeypatch, capsys, envelope):
    from hermes_cli import kanban_origin_native as native
    monkeypatch.setattr(native, 'lookup', lambda *a, **kw: None)
    (world[0] / 'kanban/origin-ledger.db').unlink()
    for _ in range(2):
        result = helpers.invoke(world, monkeypatch, capsys, envelope)
        assert result['status'] == 'rejected' and result['code'] == 'origin_forward_binding_missing'
        assert result['handoff_complete'] is False and 'outbound' not in result
        rows = notice_rows(world)
        assert len(rows) == 1
        session, text, metadata = rows[0]
        assert session == 's_new'
        assert envelope['id'] in text and 't_one' in text
        assert 'not created' in text and 'No execution' in text
        assert json.loads(metadata)['notice_id'].startswith('origin-forward-rejected:')
    assert not (world[0] / 'kanban/origin-ledger.db').exists()
    with sqlite3.connect(world[0] / 'kanban/origin-forward.db') as db:
        assert db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 0


def test_notice_write_failure_keeps_courier_pending_until_delivery(world, monkeypatch, capsys, envelope):
    from hermes_cli import kanban_origin_native as native
    monkeypatch.setattr(native, 'lookup', lambda *a, **kw: None)
    (world[0] / 'kanban/origin-ledger.db').unlink()
    real = SessionDB.record_kanban_notice

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('controlled unavailable notice store')

    monkeypatch.setattr(SessionDB, 'record_kanban_notice', fail)
    result = helpers.invoke(world, monkeypatch, capsys, envelope)
    assert result['status'] == 'pending' and not result['handoff_complete']
    assert notice_rows(world) == []
    monkeypatch.setattr(SessionDB, 'record_kanban_notice', real)
    assert helpers.invoke(world, monkeypatch, capsys, envelope)['status'] == 'rejected'
    assert len(notice_rows(world)) == 1


def test_active_turn_retries_without_interrupting_then_delivers(world, monkeypatch, capsys, envelope):
    import os
    from hermes_state_pidns import holder_namespace_token
    from hermes_cli import kanban_origin_native as native
    monkeypatch.setattr(native, 'lookup', lambda *a, **kw: None)
    (world[0] / 'kanban/origin-ledger.db').unlink()
    with SessionDB(world[0] / 'state.db') as db:
        holder = f'pid={os.getpid()}{holder_namespace_token()}:turn=s_new:notice-test:platform=desktop'
        assert db.try_acquire_session_turn_lease('s_new', holder)
        try:
            result = helpers.invoke(world, monkeypatch, capsys, envelope)
            assert result['status'] == 'pending' and result['code'] == 'origin_forward_notice_pending'
            assert notice_rows(world) == []
        finally:
            db.release_session_turn_lease('s_new', holder)
    assert helpers.invoke(world, monkeypatch, capsys, envelope)['status'] == 'rejected'
    assert len(notice_rows(world)) == 1


def test_other_refusal_does_not_write_notice(world, monkeypatch, capsys, envelope):
    from hermes_cli import kanban_sync_client as sync
    monkeypatch.setattr(sync.Hub, 'snapshot_page', lambda *a, **kw: {
        'table': 'leases', 'rows': [], 'more': False})
    result = helpers.invoke(world, monkeypatch, capsys, envelope)
    assert result['code'] == 'origin_forward_lease_invalid'
    assert notice_rows(world) == []


def test_delegated_notice_store_fence_refuses_before_effects(world, monkeypatch, capsys):
    import argparse
    from hermes_cli import kanban
    monkeypatch.setattr('agent.delegation_context.kanban_path_is_fenced',
                        lambda path: path == world[0] / 'state.db')
    args = argparse.Namespace(kanban_action='origin-forward', request_file='-')
    assert kanban.kanban_command(args) != 0
    assert notice_rows(world) == []
    assert not (world[0] / 'kanban/origin-forward.db').exists()


@pytest.mark.parametrize('mode', ['missing_tip', 'wrong_profile', 'vanished_lineage'])
def test_destination_race_never_finalizes_a_silent_rejection(world, monkeypatch, envelope, mode):
    from hermes_cli import kanban_origin_adapter as origin, kanban_origin_forward_notice as notice
    from hermes_constants import get_hermes_home
    if mode == 'vanished_lineage':
        def missing(*args, **kwargs):
            raise ValueError('origin_session_missing')
        monkeypatch.setattr(origin, '_resolve_session', missing)
    elif mode == 'missing_tip':
        monkeypatch.setattr(SessionDB, 'get_session', lambda *a, **kw: None)
    else:
        monkeypatch.setattr(SessionDB, 'get_session', lambda *a, **kw: {'profile_name': 'foreign'})
    with pytest.raises(ValueError, match='origin_forward_notice_pending'):
        notice.publish(envelope, home=get_hermes_home(), gateway='origin', profile='default')
    assert notice_rows(world) == []


def test_successful_forward_has_no_rejection_notice(world, monkeypatch, capsys, envelope):
    assert helpers.invoke(world, monkeypatch, capsys, envelope)['status'] == 'prepared'
    assert notice_rows(world) == []
