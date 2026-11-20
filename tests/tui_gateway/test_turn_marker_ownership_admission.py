"""[daveey] A prompt.admit whose dispatch fails never retires a marker it did not write (#3840 review).

The dispatch never started a turn, so no marker on disk is this admission's: a sibling record's live turn (or a
paused turn a resume will continue) keeps its marker. Before, the admission_dispatch_failed frame retired by
session_key.
"""
from tests.tui_gateway.test_prompt_admission import call
from tui_gateway import server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

pytest_plugins = ['tests.tui_gateway.test_prompt_admission']


def test_failed_dispatch_keeps_a_sibling_turns_marker(owner, monkeypatch):
    _db, session, params, _started = owner
    home, key = server._session_home(session), str(session["session_key"])
    record_turn_start(home, key, "a sibling record's live turn")
    monkeypatch.setattr(server, '_start_session_work', lambda *a, **kw: None)  # the dispatch fails
    receipt = call('prompt.admit', params)['result']
    assert receipt['dispatch'] == 'cancelled'
    assert read_turn_marker(home, key)["prompt"] == "a sibling record's live turn"
