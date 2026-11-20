"""A managed switch that resolves while a turn is admitted must not publish a stale turn lease holder."""
import threading
import uuid
from unittest.mock import MagicMock, patch

import pytest

from agent import agent_runtime_helpers as runtime
from agent.turn_facade_lease import admit_durable_turn_lease
from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError
from tests.agent import test_managed_subscription as fixtures
from tests.agent import test_managed_switch as switch_fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
destinations = switch_fixtures.destinations
build = switch_fixtures.build

SESSION, TARGET = 'pin', 'z-anthropic-deep'


@pytest.fixture
def live(destinations, tmp_path):
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        agent = build('class:deep', SESSION)
        db = SessionDB(tmp_path / 'state.db')
        db.create_session(SESSION, source='desktop')
        agent._session_db = db
        agent.platform = 'desktop'
        try:
            yield agent, db
        finally:
            db.close()


def admit(agent):
    lease = admit_durable_turn_lease(  # holders are per turn, as turn_facade's uuid makes them
        agent, session_id=SESSION, relay_turn_id=f'{SESSION}:fixture:{uuid.uuid4().hex[:8]}',
        task_context={'platform': 'desktop', 'session_id': SESSION}, conversation_history=[]).lease
    assert lease is not None
    return lease


class PausedSwitch:
    """Run the real switch on another thread, parked after its snapshot and before it publishes."""

    def __init__(self, agent):
        self.snapshotted, self.resume, self.errors = threading.Event(), threading.Event(), []
        original = runtime._update_switch_compressor

        def parked(*args):
            self.snapshotted.set()
            assert self.resume.wait(30)
            return original(*args)

        def run():
            try:
                with patch.object(runtime, '_update_switch_compressor', side_effect=parked):
                    agent.switch_model(TARGET, 'anthropic')
            except BaseException as exc:  # reported by finish()
                self.errors.append(exc)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert self.snapshotted.wait(30)

    def finish(self):
        self.resume.set()
        self.thread.join(30)
        assert not self.thread.is_alive() and self.errors == []


def flush(agent, db, text):
    """The persist path's own arguments (agent.session_persistence._db_flush_write)."""
    db.append_messages_batch(
        session_id=agent.session_id, messages=[{'role': 'assistant', 'content': text}],
        turn_lease_holder=agent._active_session_turn_lease_holder,
        turn_lease_ttl_seconds=agent._active_session_turn_lease_ttl_seconds or 300.0)


def contents(db):
    return [row['content'] for row in db.get_messages(SESSION)]


def test_turn_admitted_during_switch_keeps_its_lease(live):
    agent, db = live
    admit(agent).release()                # an earlier turn: the agent now carries a cleared holder
    switch = PausedSwitch(agent)          # snapshot: no turn, holder None
    lease = admit(agent)                  # the unlocked running check lost the race
    switch.finish()
    try:
        assert agent.model == TARGET      # the switch itself still lands
        assert agent._active_session_turn_lease_holder == lease.holder
        assert agent._last_session_turn_lease_holder == lease.holder
        flush(agent, db, 'reply')
    finally:
        lease.release()
    assert contents(db) == ['reply']
    assert agent._active_session_turn_lease_holder is None
    with pytest.raises(SessionTurnLeaseLostError, match='missing lease row'):
        db.append_message(SESSION, role='assistant', content='late', turn_lease_holder=lease.holder)
    assert contents(db) == ['reply']


def test_switch_snapshotted_in_one_turn_does_not_install_its_holder_in_the_next(live):
    agent, db = live
    first = admit(agent)
    switch = PausedSwitch(agent)          # snapshot carries the first turn's holder
    first.release()
    second = admit(agent)
    switch.finish()
    try:
        assert agent._active_session_turn_lease_holder == second.holder
        flush(agent, db, 'second reply')  # base: refused, "holder mismatch"
    finally:
        second.release()
    assert contents(db) == ['second reply']
    with pytest.raises(SessionTurnLeaseLostError):
        db.append_message(SESSION, role='assistant', content='stale', turn_lease_holder=first.holder)
    assert contents(db) == ['second reply']


def test_switch_after_release_does_not_resurrect_the_holder(live):
    agent, db = live
    lease = admit(agent)
    switch = PausedSwitch(agent)          # snapshot carries the live holder
    lease.release()
    switch.finish()
    assert agent.model == TARGET
    assert agent._active_session_turn_lease_holder is None
    with pytest.raises(SessionTurnLeaseLostError, match='missing lease row'):
        db.append_message(SESSION, role='assistant', content='late', turn_lease_holder=lease.holder)
    assert contents(db) == []


def test_quiet_switch_publishes_the_same_runtime(live):
    agent, db = live
    agent.switch_model(TARGET, 'anthropic')
    assert agent.model == TARGET and agent._session_db is db
    assert getattr(agent, '_active_session_turn_lease_holder', None) is None


@pytest.mark.parametrize('field, during, expected', [
    ('_stream_options_unsupported', True, False),
    ('_fallback_activated', True, False),
    ('_cached_system_prompt', 'prompt built for the old model', None),
])
def test_switch_reset_lands_over_a_value_set_during_the_switch(live, field, during, expected):
    """The reset equals the snapshot value, so a publish that diffs against the snapshot would drop it."""
    agent, _db = live
    setattr(agent, field, expected)
    switch = PausedSwitch(agent)
    setattr(agent, field, during)
    switch.finish()
    assert getattr(agent, field) == expected and agent.model == TARGET
