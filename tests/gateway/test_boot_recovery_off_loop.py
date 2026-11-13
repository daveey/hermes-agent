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
    import gateway.session_native_recovery_routes as recovery_routes

    def recovered_source(runner, owner_authority, entry, owner):
        assert owner_authority is authority and owner == 'root'
        calls.append(('recovered_source', threading.get_ident()))
        return entry.origin + ':recovered'

    monkeypatch.setattr(recovery_routes, 'recovered_native_source', recovered_source)
    assert await recover_profile_native_sessions(_runner(calls), authority) == {}

    assert authority.bindings == [('root', 'origin-a:recovered', 'adapter:origin-a:recovered')]
    assert {name for name, _ in calls} == {'read_all', 'list_sessions', 'logical_owner', 'recovered_source'}
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
