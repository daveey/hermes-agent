"""Admission projects its committed row before output, never on cancelled dispatch."""
from tests.tui_gateway.test_prompt_admission import call, owner
from tui_gateway import server


def test_committed_user_event_precedes_turn_and_retry_does_not_reemit(owner, monkeypatch):
    db, session, params, started = owner
    events = []
    monkeypatch.setattr(server, '_emit', lambda *args, **kwargs: events.append(args))

    def run(*args, **kwargs):
        assert events[0][0] == 'message.admitted'
        assert db.get_messages(params['stored_session_id'])[0]['content'] == params['text']

    monkeypatch.setattr(server, '_run_after_agent_ready', run)
    receipt = call('prompt.admit', params)['result']
    started[0]()
    event = next(e for e in events if e[0] == 'message.admitted')
    assert server._sessions[event[1]] is session
    assert event[2] == {
        'stored_session_id': params['stored_session_id'],
        'profile': params['profile'],
        'message_uid': receipt['message_uid'],
        'user_row_id': receipt['user_row_id'],
        'text': params['text'],
        'timestamp': session['_submit_user_row']['timestamp'],
    }
    assert call('prompt.admit', params)['result'] == receipt
    assert len([e for e in events if e[0] == 'message.admitted']) == 1
    assert len(started) == 1


def test_cancelled_dispatch_never_projects_permission(owner, monkeypatch):
    _, _, params, _ = owner
    events = []
    callbacks = []
    monkeypatch.setattr(server, '_emit', lambda *args, **kwargs: events.append(args))

    def refuse(target, **kwargs):
        callbacks.append(target)

    monkeypatch.setattr(server, '_start_session_work', refuse)
    assert call('prompt.admit', params)['result']['dispatch'] == 'cancelled'
    callbacks[0]()
    assert not any(e[0] == 'message.admitted' for e in events)


def test_admitted_event_uses_owner_transport_and_real_contract(owner, monkeypatch):
    from types import SimpleNamespace

    _, session, params, started = owner
    owner_frames, foreign_frames = [], []
    session['transport'] = SimpleNamespace(write=lambda frame: owner_frames.append(frame) or True)
    foreign = SimpleNamespace(write=lambda frame: foreign_frames.append(frame) or True)
    receipt = call('prompt.admit', params)['result']
    # A callback's ambient transport must not steal the session's event.
    monkeypatch.setattr(server, 'current_transport', lambda: foreign)

    def run(*args, **kwargs):
        frame = owner_frames[0]['params']
        assert frame['type'] == 'message.admitted'
        assert frame['payload']['message_uid'] == receipt['message_uid']
        assert frame['payload']['profile'] == params['profile']
        assert frame['seq'] > 0

    monkeypatch.setattr(server, '_run_after_agent_ready', run)
    started[0]()
    assert len(owner_frames) == 1
    assert not foreign_frames
