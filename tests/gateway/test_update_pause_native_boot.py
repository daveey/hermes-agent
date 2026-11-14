"""Boot finds durable started checkpoints without replaying ordinary started turns."""
from tests.hermes_state.raw_rows import raw_append_message
import pytest


async def _started(tmp_path, monkeypatch, *, checkpoint=True, detached=False):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_ingress_context import native_callback, register_transport_home
    from hermes_state_runtime import claim_session_input
    from hermes_state_update_pause import checkpoint_session_input
    from tests.gateway.test_command_bypass_active_session import _StubAdapter

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    monkeypatch.setattr('gateway.run._hermes_home', tmp_path)
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', tmp_path / 'state.db')
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=False, sessions_dir=tmp_path / 'sessions'))
    adapter = _StubAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False), Platform.TELEGRAM)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._wire_adapter_handlers(adapter)
    register_transport_home(runner, None, tmp_path)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    old = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='old')
    old._schedule = lambda ref: None
    event = MessageEvent(text='original', source=source, message_id='head')
    with native_callback(runner, event, tmp_path):
        receipt = await old.admit_native(event)
    sid = receipt.ref.session_id
    row = claim_session_input(old.db, epoch=old.epoch, session_id=sid)
    old.db.append_messages_batch(sid, [
        {'role': 'user', 'content': 'original'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call-a', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
        {'role': 'tool', 'content': 'done', 'tool_call_id': 'call-a', 'tool_name': 'example'},
    ])
    if checkpoint:
        checkpoint_session_input(old.db, epoch=old.epoch, admission_id=row['admission_id'],
            generation=row['generation'], physical_session_id=sid, transcript_mark=row['transcript_mark'])
    if detached:
        runner.session_store.get_or_create_session(source, force_new=True)
    return runner, old, receipt.ref, row


@pytest.mark.asyncio
@pytest.mark.parametrize('detached', [False, True], ids=['native', 'detached'])
async def test_boot_schedules_started_checkpoint_without_queued_follower(tmp_path, monkeypatch, detached):
    from gateway.run_runtime import recover_profile_native_sessions
    from gateway.session_authority import initialize_session_authority
    from hermes_state_runtime import get_session_admission, list_session_admissions
    from hermes_state_update_pause import get_session_checkpoint

    runner, _old, ref, row = await _started(tmp_path, monkeypatch, detached=detached)
    try:
        cold = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='replacement')
        scheduled = []
        cold._schedule = scheduled.append
        assert get_session_admission(cold.db, admission_id=row['admission_id'])['status'] == 'started'
        assert get_session_checkpoint(cold.db, session_id=ref.session_id) is not None
        assert [r['status'] for r in list_session_admissions(cold.db, session_id=ref.session_id)] == ['started']
        outcome = await recover_profile_native_sessions(runner, cold)
        assert outcome == {ref.session_id: 'detached' if detached else 'ready'}
        assert scheduled == [ref]
        assert cold.sessions[ref.session_id].detached is detached
        # Boot only mounts/schedules: the receipt survives until the guarded claim.
        assert get_session_checkpoint(cold.db, session_id=ref.session_id)['admission_id'] == row['admission_id']
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('detached', [False, True], ids=['native', 'detached'])
@pytest.mark.parametrize('invalid_receipt', [False, True], ids=['no-receipt', 'stale-receipt'])
async def test_boot_does_not_schedule_ordinary_current_started_turn(tmp_path, monkeypatch, detached, invalid_receipt):
    from gateway.run_runtime import recover_profile_native_sessions
    from hermes_state_runtime import get_session_admission

    runner, owner, ref, row = await _started(tmp_path, monkeypatch, checkpoint=invalid_receipt, detached=detached)
    try:
        if invalid_receipt:
            raw_append_message(owner.db, ref.session_id, 'assistant', 'boundary changed')
        from hermes_state_update_pause import get_session_checkpoint
        assert get_session_checkpoint(owner.db, session_id=ref.session_id) is None
        scheduled = []
        owner._schedule = scheduled.append
        owner.sessions.clear()  # discovery must not treat started as queued, even if not mounted
        assert await recover_profile_native_sessions(runner, owner) == {}
        assert scheduled == []
        assert get_session_admission(owner.db, admission_id=row['admission_id'])['status'] == 'started'
    finally:
        runner.session_store.close_all_db_handles()
