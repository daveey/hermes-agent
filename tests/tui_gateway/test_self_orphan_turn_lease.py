"""Failed release must not turn this agent's ended turn into a foreign owner."""
import contextlib
import os
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agent.turn_facade_lease import DurableTurnLease
from hermes_state import SessionDB
from hermes_state_pidns import holder_namespace_token
from tui_gateway import server


@pytest.fixture
def orphan(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('root', source='desktop')
    holder = f'pid={os.getpid()}{holder_namespace_token()}:turn=ended:platform=desktop'
    assert db.try_acquire_session_turn_lease('root', holder)
    agent = SimpleNamespace(_session_db=db, session_id='root',
                            _active_session_turn_lease_holder=holder, clear_interrupt=lambda: None)
    lease = DurableTurnLease(agent, db, 'root', holder)
    release = db.release_session_turn_lease
    def failed(*args, **kwargs):
        raise sqlite3.DatabaseError('injected unavailable database')
    monkeypatch.setattr(db, 'release_session_turn_lease', failed)
    lease.release()
    assert agent._active_session_turn_lease_holder is None
    monkeypatch.setattr(db, 'release_session_turn_lease', release)
    session = {'session_key': 'root', 'agent': agent, 'history_lock': threading.Lock(),
               'history': [], 'running': True, '_synthesized_turn': True, 'attached_images': [],
               'active_session_lease': object()}
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_session_moved_refusal', lambda _: None)
    monkeypatch.setattr(server, 'admit_synthesized_turn', lambda _: True)
    monkeypatch.setattr(server, '_start_inflight_turn', lambda *a, **k: None)
    errors = []
    monkeypatch.setattr(server, '_emit_terminal_turn_error', lambda *a, **k: errors.append(a[2]))
    yield SimpleNamespace(db=db, agent=agent, lease=lease, holder=holder, session=session,
                          release=release, errors=errors, fail=failed)
    db.close()


def test_synthesized_admission_settles_exact_failed_release(orphan):
    state = orphan
    assert state.db.session_turn_lease_owner('root')['holder'] == state.holder
    result = server._admit_prompt_turn('ui', state.session, 'notice', [], None, None, None)
    assert result == ([], state.agent)
    assert state.errors == []
    assert state.db.session_turn_lease_owner('root') is None
    assert state.db.try_acquire_session_turn_lease('root', 'next-turn')


def test_still_failed_store_is_coordination_failure_with_self_provenance(orphan, monkeypatch):
    monkeypatch.setattr(orphan.db, 'release_session_turn_lease', orphan.fail)
    refusal = server._durable_owner_refusal(orphan.session)
    assert getattr(refusal, 'reason', None) == 'SESSION_COORDINATION_UNAVAILABLE'
    assert refusal.ownership_cause == 'lease_self_orphan'
    assert refusal.holder_pid == os.getpid()
    assert orphan.db.session_turn_lease_owner('root')['holder'] == orphan.holder
    monkeypatch.setattr(orphan.db, 'release_session_turn_lease', orphan.release)
    assert server._durable_owner_refusal(orphan.session) is None


def test_different_holder_even_in_same_process_stays_fenced(orphan, monkeypatch):
    orphan.release('root', orphan.holder)
    foreign = f'pid={os.getpid()}{holder_namespace_token()}:turn=foreign:platform=desktop'
    assert orphan.db.try_acquire_session_turn_lease('root', foreign)
    calls = []
    monkeypatch.setattr(orphan.db, 'release_session_turn_lease', lambda *args: calls.append(args))
    refusal = server._durable_owner_refusal(orphan.session)
    assert calls == []
    assert refusal.reason == 'SESSION_NOT_OWNED'
    assert refusal.ownership_cause == 'lease_unknown'
    assert refusal.holder_pid == 0
    assert orphan.db.session_turn_lease_owner('root')['holder'] == foreign


@pytest.mark.parametrize('boundary', ['agent', 'process', 'store', 'missing_store', 'receipt_type'])
def test_receipt_cannot_authorize_a_different_custodian(orphan, monkeypatch, tmp_path, boundary):
    from dataclasses import replace
    receipt = orphan.agent._failed_turn_release
    if boundary == 'agent':
        orphan.session['agent'] = SimpleNamespace()
    elif boundary == 'process':
        orphan.agent._failed_turn_release = replace(receipt, pid=-1)
    elif boundary == 'receipt_type':
        orphan.agent._failed_turn_release = SimpleNamespace(**receipt.__dict__)
    else:
        # Adapter reports another/missing store while retaining real row reads.
        proxy = SimpleNamespace(db_path=None if boundary == 'missing_store' else tmp_path / 'other.db',
                                session_turn_lease_owner=orphan.db.session_turn_lease_owner,
                                release_session_turn_lease=orphan.release)
        monkeypatch.setattr(server, '_session_db', lambda _: contextlib.nullcontext(proxy))
    calls = []
    target = proxy if boundary in ('store', 'missing_store') else orphan.db
    monkeypatch.setattr(target, 'release_session_turn_lease', lambda *args: calls.append(args))
    refusal = server._durable_owner_refusal(orphan.session)
    assert refusal.reason == 'SESSION_NOT_OWNED'
    assert refusal.ownership_cause == 'lease_unknown'
    assert calls == []


def test_successor_racing_settlement_is_re_read_and_fenced(orphan, monkeypatch):
    def racing_release(session_id, holder):
        orphan.release(session_id, holder)
        assert orphan.db.try_acquire_session_turn_lease('root', 'successor')
        orphan.release(session_id, holder)  # old holder cannot remove successor
    monkeypatch.setattr(orphan.db, 'release_session_turn_lease', racing_release)
    refusal = server._durable_owner_refusal(orphan.session)
    assert refusal.reason == 'SESSION_NOT_OWNED'
    assert orphan.db.session_turn_lease_owner('root')['holder'] == 'successor'


def test_unidentified_durable_holder_defers_without_releasing_it(orphan, monkeypatch):
    orphan.release('root', orphan.holder)
    assert orphan.db.try_acquire_session_turn_lease('root', 'opaque-holder')
    calls = []
    monkeypatch.setattr(orphan.db, 'release_session_turn_lease', lambda *a: calls.append(a))
    assert server._admit_prompt_turn('ui', orphan.session, 'notice', [], None, None, None) is None
    assert orphan.errors == []
    assert calls == []
    assert orphan.db.session_turn_lease_owner('root')['holder'] == 'opaque-holder'


def test_live_exact_holder_is_not_settled(orphan, monkeypatch):
    orphan.agent._active_session_turn_lease_holder = orphan.holder
    calls = []
    monkeypatch.setattr(orphan.db, 'release_session_turn_lease', lambda *args: calls.append(args))
    assert server._durable_owner_refusal(orphan.session) is None
    assert calls == []
    assert orphan.db.session_turn_lease_owner('root')['holder'] == orphan.holder


def test_compressed_tip_settles_original_root_only(orphan):
    orphan.db.end_session('root', 'compression')
    orphan.db.create_session('tip', source='desktop', parent_session_id='root')
    orphan.session['session_key'] = 'tip'
    assert server._durable_owner_refusal(orphan.session) is None
    assert orphan.db.session_turn_lease_owner('tip') is None


def test_unreadable_store_does_not_use_receipt_as_admission(orphan, monkeypatch):
    monkeypatch.setattr(orphan.db, 'session_turn_lease_owner', orphan.fail)
    assert server._durable_owner_refusal(orphan.session) is not None
    assert orphan.agent._failed_turn_release is not None


def test_unknown_store_release_failure_does_not_create_receipt():
    def unavailable(*args):
        raise sqlite3.DatabaseError('injected unavailable database')
    db = SimpleNamespace(db_path=None, release_session_turn_lease=unavailable)
    agent = SimpleNamespace(_active_session_turn_lease_holder='holder')
    DurableTurnLease(agent, db, 'root', 'holder').release()
    assert getattr(agent, '_failed_turn_release', None) is None
    assert agent._active_session_turn_lease_holder is None


def test_successful_release_never_creates_recovery_authority(tmp_path):
    db = SessionDB(tmp_path / 'healthy.db')
    try:
        assert db.try_acquire_session_turn_lease('root', 'holder')
        agent = SimpleNamespace(_active_session_turn_lease_holder='holder')
        lease = DurableTurnLease(agent, db, 'root', 'holder')
        lease.release()
        assert getattr(agent, '_failed_turn_release', None) is None
    finally:
        db.close()
