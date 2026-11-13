"""Gateway boot must not run per-routing-entry state.db reads on the event loop.

On the hub (16k routing entries) ``recover_profile_native_sessions`` resolved every route's
logical owner with a full ``get_session`` row (plus the ~45 KB joined system prompt) on the loop,
and the startup prune did the same per entry: loop-watch logged 3-7 s stalls with
``loop top=hermes_state.py:1226 <lambda>`` at every gateway boot.
"""

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionStore


class _Db:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def _read_all(self, sql, params=()):
        self.calls.append(('read_all', threading.get_ident()))
        return self.rows


class _Authority:
    profile_id = 'fixture'

    def __init__(self, rows, calls):
        self.db = _Db(rows, calls)
        self.calls = calls
        self.sessions = {}
        self.bindings = None

    def logical_owner(self, session_id):
        self.calls.append(('logical_owner', threading.get_ident()))
        return 'root'

    async def recover_native_sessions(self, bindings):
        self.bindings = bindings
        return {}


def _runner(calls):
    entries = [SimpleNamespace(session_id='tip', origin='origin-a'), SimpleNamespace(session_id='x', origin=None)]

    def list_sessions():
        calls.append(('list_sessions', threading.get_ident()))
        return entries
    return SimpleNamespace(session_store=SimpleNamespace(list_sessions=list_sessions),
                           _adapter_for_source=lambda origin: f'adapter:{origin}')


@pytest.mark.asyncio
async def test_native_recovery_resolves_route_owners_off_the_loop(monkeypatch):
    import gateway.session_detached as detached
    from gateway.run_runtime import recover_profile_native_sessions
    monkeypatch.setattr(detached, 'recover_detached_sessions', lambda authority, pending: [])
    calls = []
    authority = _Authority([{'target_session_id': 'root', 'status': 'queued'}], calls)
    loop_thread = threading.get_ident()

    assert await recover_profile_native_sessions(_runner(calls), authority) == {}

    assert authority.bindings == [('root', 'origin-a', 'adapter:origin-a')]
    assert {name for name, _ in calls} == {'read_all', 'list_sessions', 'logical_owner'}
    assert [name for name, thread in calls if thread == loop_thread] == []


@pytest.mark.asyncio
async def test_native_recovery_skips_route_scan_when_nothing_is_pending(monkeypatch):
    import gateway.session_detached as detached
    from gateway.run_runtime import recover_profile_native_sessions
    monkeypatch.setattr(detached, 'recover_detached_sessions', lambda authority, pending: [])
    calls = []
    authority = _Authority([], calls)

    assert await recover_profile_native_sessions(_runner(calls), authority) == {}

    assert authority.bindings == []
    assert [name for name, _ in calls] == ['read_all']


def test_startup_prune_reads_end_reasons_in_batches_not_full_rows(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        for sid in ('ended_a', 'ended_b', 'live_c'):
            db.create_session(sid, source='telegram')
        db.end_session('ended_a', 'agent_close')
        db.end_session('ended_b', 'session_reset')
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(SessionStore, '_ensure_loaded', lambda self: None)
            store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        store._db = db
        store._loaded = True
        now = datetime.now()
        for key, sid in (('ka', 'ended_a'), ('kb', 'ended_b'), ('kc', 'live_c'), ('kd', 'missing_d')):
            store._entries[key] = SessionEntry(
                session_key=key, session_id=sid, created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1), platform=Platform.TELEGRAM, chat_type='dm')
        full_rows = []
        monkeypatch.setattr(type(db), 'get_session', lambda self, sid: full_rows.append(sid))
        monkeypatch.setattr(store, '_save', lambda: None)

        store._prune_stale_sessions_locked()

        assert full_rows == []
        assert sorted(store._entries) == ['kc', 'kd']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_recovery_binds_only_routes_with_pending_owners(monkeypatch):
    import gateway.session_detached as detached
    from gateway.run_runtime import recover_profile_native_sessions

    monkeypatch.setattr(detached, 'recover_detached_sessions', lambda authority, pending: [])
    authority = _Authority([
        {'target_session_id': 'queued-root', 'status': 'queued'},
        {'target_session_id': 'unknown-root', 'status': 'unknown'},
    ], [])
    owners = {'queued-tip': 'queued-root', 'unknown-tip': 'unknown-root', 'idle-tip': 'idle-root'}
    monkeypatch.setattr(authority, 'logical_owner', owners.__getitem__)
    entries = [SimpleNamespace(session_id=sid, origin=sid) for sid in owners]
    adapters = []

    def adapter_for_source(origin):
        adapters.append(origin)
        return f'adapter:{origin}'

    runner = SimpleNamespace(
        session_store=SimpleNamespace(list_sessions=lambda: entries),
        _adapter_for_source=adapter_for_source,
    )
    assert await recover_profile_native_sessions(runner, authority) == {}
    assert authority.bindings == [
        ('queued-root', 'queued-tip', 'adapter:queued-tip'),
        ('unknown-root', 'unknown-tip', 'adapter:unknown-tip'),
    ]
    assert adapters == ['queued-tip', 'unknown-tip']


def _prune_store(tmp_path, monkeypatch):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SessionStore, '_ensure_loaded', lambda self: None)
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._loaded = True
    monkeypatch.setattr(store, '_save', lambda: None)
    return store


def _route(key, sid):
    now = datetime.now()
    return SessionEntry(session_key=key, session_id=sid, created_at=now,
                        updated_at=now, platform=Platform.TELEGRAM, chat_type='dm')


def test_startup_prune_processes_ended_sessions_beyond_first_chunk(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_state_common import _SQL_IN_CHUNK

    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        store = _prune_store(tmp_path, monkeypatch)
        store._db = db
        # Missing routes fill the first batch; an ended row in each batch makes
        # truncation visible without manufacturing hundreds of session writes.
        ids = [f'missing-{i}' for i in range(_SQL_IN_CHUNK - 1)]
        ids += ['ended-first', 'ended-second', 'live-second', 'missing-second']
        for sid in ('ended-first', 'ended-second', 'live-second'):
            db.create_session(sid, source='telegram')
        for sid in ('ended-first', 'ended-second'):
            db.end_session(sid, 'agent_close')
        store._entries = {sid: _route(sid, sid) for sid in ids}

        store._prune_stale_sessions_locked()

        assert set(store._entries) == set(ids) - {'ended-first', 'ended-second'}
    finally:
        db.close()


@pytest.mark.parametrize('first_profile', ['alpha', 'beta'])
def test_startup_prune_keeps_same_id_rows_isolated_by_profile(tmp_path, monkeypatch, first_profile):
    from contextlib import ExitStack
    from hermes_state import SessionDB

    with ExitStack() as cleanup:
        dbs = {}
        for profile in ('alpha', 'beta'):
            db = SessionDB(db_path=tmp_path / f'{profile}.db')
            cleanup.callback(db.close)
            dbs[profile] = db
            for sid in ('shared-a', 'shared-b'):
                db.create_session(sid, source='telegram')
        dbs['alpha'].end_session('shared-a', 'agent_close')
        dbs['beta'].end_session('shared-b', 'session_reset')
        store = _prune_store(tmp_path, monkeypatch)
        # Supply owned DB handles at the resolver seam; the real batch queries
        # and stale-route verdicts must preserve their independent row states.
        key_dbs = {}
        profiles = [first_profile, 'beta' if first_profile == 'alpha' else 'alpha']
        for profile in profiles:
            for sid in ('shared-a', 'shared-b'):
                key = f'{profile}:{sid}'
                store._entries[key] = _route(key, sid)
                key_dbs[key] = dbs[profile]
        monkeypatch.setattr(store, '_db_for_key', key_dbs.__getitem__)

        store._prune_stale_sessions_locked()

        assert set(store._entries) == {'alpha:shared-b', 'beta:shared-a'}
