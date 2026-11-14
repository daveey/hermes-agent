"""A retry must not resurrect a replay plan another owner finished."""
import pytest

from tests.gateway.test_wall_replay_automation_wake import _authority, _intents, _seed, _stop


@pytest.mark.asyncio
async def test_retry_does_not_resurrect_a_concurrently_deleted_plan(tmp_path, monkeypatch):
    from gateway import session_automation, session_wall_replay as replay
    from hermes_state_runtime import RuntimeStoreError

    runner, db, authority = await _authority(tmp_path, monkeypatch)
    calls = []
    try:
        intent = _seed(db)
        key = replay.PREFIX + intent['admission_id']

        async def admit(*args, **kwargs):
            # fire_replay has read the plan; another owner now finishes and deletes it
            # before the stale owner's admission refusal reaches retry persistence.
            assert replay._get(db, key) is not None
            replay._write(db, key, None)
            assert replay._get(db, key) is None
            calls.append('deleted')
            raise RuntimeStoreError('stale_generation')

        monkeypatch.setattr(session_automation, 'admit_route_wake', admit)
        monkeypatch.setattr(runner, '_adapter_for_source', lambda source: object())
        assert await replay.fire_replay(authority, intent['admission_id']) == 'retry'
        assert calls == ['deleted']
        assert replay._get(db, key) is None
        assert _intents(db) == {}
        assert await replay.fire_replay(authority, intent['admission_id']) == 'gone'
        assert calls == ['deleted']
    finally:
        await _stop(authority, db)
