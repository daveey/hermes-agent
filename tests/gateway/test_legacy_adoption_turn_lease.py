"""A live DB turn lease / compression lock on an old chat is a typed busy refusal, never -32603.

Self-heal gateway-56086c8e65: Desktop's ``session.resume`` of a not-yet-adopted local chat crashed
with ``SessionTurnLeaseLostError`` (ws dispatch crash, JSON-RPC -32603) while another writer held a
live turn lease that had no active-session registry entry.
"""
import logging
from types import SimpleNamespace

import pytest


async def _authority(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store._db
    db.create_session('old-leased', source='cli', model='old-model', cwd=str(tmp_path))
    db.append_message('old-leased', role='user', content='OLD_QUESTION')
    db.end_session('old-leased', 'cli_exit')
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='first')
    native = AuthorityConnection(authority, object(), {'provider': 'local', 'user_id': 'native',
        'profile_id': str(tmp_path), 'instance_id': 'first'})
    return db, native


def _policy_rows(db):
    with db._read_ctx() as conn:
        return conn.execute("SELECT key FROM state_meta WHERE key LIKE 'gateway.local_policy.v1:%' "
                            "OR key LIKE 'gateway.local_legacy%'").fetchall()


@pytest.mark.asyncio
async def test_resume_of_old_chat_under_live_turn_lease_is_typed_refusal(tmp_path, monkeypatch, caplog):
    db, native = await _authority(tmp_path, monkeypatch)
    # Another process's live turn (no active-session registry entry: only the DB lease).
    assert db.acquire_session_turn_lease('old-leased', 'other-writer', ttl_seconds=300, wait_seconds=0)
    with caplog.at_level(logging.WARNING, logger='gateway.session_controls'):
        refused = await native.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 'old-leased'}})
    # Translated at the adoption path itself, not by the dispatcher backstop.
    assert not [r for r in caplog.records if 'untranslated mutation guard refusal' in r.getMessage()]
    assert 'error' in refused, refused
    assert refused['error']['code'] == 4001, refused
    assert refused['error']['message'] == 'runtime_coordination_required', refused
    assert not _policy_rows(db)
    assert db.get_session('old-leased')['source'] == 'cli'
    db.release_session_turn_lease('old-leased', 'other-writer')
    resumed = await native.dispatch({'id': 2, 'method': 'session.resume', 'params': {'session_id': 'old-leased'}})
    assert resumed.get('result', {}).get('session_id') == 'old-leased', resumed
    assert [m['content'] for m in resumed['result']['messages']] == ['OLD_QUESTION']


@pytest.mark.asyncio
async def test_resume_of_old_chat_under_compression_lock_is_session_busy(tmp_path, monkeypatch, caplog):
    db, native = await _authority(tmp_path, monkeypatch)
    assert db.try_acquire_compression_lock('old-leased', 'other-compressor', ttl_seconds=300)
    with caplog.at_level(logging.WARNING, logger='gateway.session_controls'):
        refused = await native.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 'old-leased'}})
    assert not [r for r in caplog.records if 'untranslated mutation guard refusal' in r.getMessage()]
    assert 'error' in refused, refused
    assert refused['error']['code'] == 4001, refused
    assert refused['error']['message'] == 'session_busy', refused
    assert refused['error']['data'].get('field') == 'compression', refused
    assert not _policy_rows(db)
    db.release_compression_lock('old-leased', 'other-compressor')
    resumed = await native.dispatch({'id': 2, 'method': 'session.resume', 'params': {'session_id': 'old-leased'}})
    assert resumed.get('result', {}).get('session_id') == 'old-leased', resumed


@pytest.mark.asyncio
@pytest.mark.parametrize('compressing', [False, True])
async def test_dispatcher_turns_an_untranslated_guard_refusal_into_busy_and_warns(
        tmp_path, monkeypatch, caplog, compressing):
    """Detection guard: a caller that forgets to translate a guard refusal still answers a
    retryable 4001 ``session_busy`` (not -32603) and logs one WARNING naming the method."""
    from gateway.session_controls import AuthorityConnection
    from hermes_state_errors import SessionCompressionInProgressError, SessionTurnLeaseLostError

    _db, native = await _authority(tmp_path, monkeypatch)
    error = SessionCompressionInProgressError if compressing else SessionTurnLeaseLostError

    async def boom(self, ref, params):
        raise error('held by another writer')

    monkeypatch.setattr(AuthorityConnection, 'resume', boom)
    with caplog.at_level(logging.WARNING, logger='gateway.session_controls'):
        reply = await native.dispatch({'id': 7, 'method': 'session.resume',
                                       'params': {'session_id': 'old-leased'}})
    assert reply.get('error', {}).get('code') == 4001, reply
    assert reply['error']['message'] == 'session_busy', reply
    assert reply['error']['data'].get('field') == ('compression' if compressing else None), reply
    warnings = [r for r in caplog.records if 'untranslated mutation guard refusal' in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert warnings[0].levelno == logging.WARNING
    assert 'method=session.resume' in warnings[0].getMessage()
