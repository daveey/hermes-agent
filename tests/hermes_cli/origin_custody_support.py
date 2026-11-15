"""Real interactive admission for native-receipt fixtures."""
from contextlib import contextmanager


@contextmanager
def interactive_capture(home, session_id):
    from agent.origin_turn_custody import scope
    from agent.turn_facade_lease import admit_durable_turn_lease
    from hermes_cli.kanban_origin_native import capture
    from hermes_state import SessionDB
    db = SessionDB(home / 'state.db')
    if db.get_session(session_id) is None:
        db.create_session(session_id, source='desktop')
    from tests.agent.test_cross_process_turn_lease import _agent_with_db
    agent = _agent_with_db(db, session_id=session_id)
    lease = admit_durable_turn_lease(agent, session_id=session_id, relay_turn_id='fixture',
        task_context={'platform': 'desktop', 'session_id': session_id}, conversation_history=[]).lease
    assert lease is not None
    try:
        lease.start()
        with scope(lease):
            value = capture(None)
            assert value is not None
            yield value
    finally:
        lease.stop_refresher()
        lease.join_threads()
        lease.release()
        db.close()
