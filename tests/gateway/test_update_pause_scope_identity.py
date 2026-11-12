"""An admission scope cannot authorize same-task adapter follow-up events."""
from types import SimpleNamespace

import pytest

from gateway.session_update_pause_ingress import pause_scope
from tests.gateway.test_turn_update_pause_real_agent import _local_turn


@pytest.fixture
def admission(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.session_tui_ops._profile_home', lambda authority: tmp_path)
    return {'admission_id': 'admitted', 'payload': {'native_text_v1': {}}, 'update_resume': True}


def _scope(row):
    return pause_scope(None, row, SimpleNamespace(message_id='update-resume:admitted'),
                       SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value='local'))),
                       is_api=False)


@pytest.mark.parametrize('message_id', ['update-resume:admitted', 'unrelated', None])
def test_production_hook_is_bound_to_runner_inbound_id(admission, message_id):
    turn = _local_turn()
    turn._ctx.inbound_message_id = message_id
    seen = []
    agent = SimpleNamespace(run_conversation=lambda *a, **kw: seen.append(
        callable(getattr(agent, 'step_boundary_pause', None))) or {'final_response': 'done'})
    with _scope(admission):
        turn._run_conversation_with_approval(agent, [], None, None, None)
    assert seen == [message_id == 'update-resume:admitted']
    assert not hasattr(agent, 'step_boundary_pause')


@pytest.mark.parametrize('message_id', ['update-resume:admitted', 'unrelated', None])
def test_resume_recovery_is_bound_to_runner_inbound_id(admission, message_id):
    turn = _local_turn()
    turn._ctx.inbound_message_id = message_id
    turn._ctx.history = [{'role': 'tool', 'content': 'completed output'}]
    turn._runner.session_store = SimpleNamespace(_entries={})
    with _scope(admission):
        turn._prepare_turn_message(turn._ctx.history)
    assert ('IGNORE those pending results' in turn._ctx.message) is (
        message_id != 'update-resume:admitted')


def test_resumed_cli_options_rebind_without_original_api_text(admission):
    from gateway.session_cli_turn import cli_turn, cli_turn_scope, one_turn_reasoning
    from tui_gateway.update_pause import pause_note
    committed = {'api_text': 'ORIGINAL API TEXT MUST NOT REPLAY',
                 'model_override': {'model': 'once-model', 'reasoning': 'high'},
                 'clarify_batch': True}
    admission['payload']['cli_turn_v1'] = committed
    turn = _local_turn()
    turn._ctx.inbound_message_id = 'update-resume:admitted'
    turn._ctx.message = pause_note('original task')
    expected_message = turn._ctx.message
    with cli_turn_scope('admitted', committed):
        with _scope(admission):
            options = cli_turn(turn._ctx.inbound_message_id)
            assert options.get('model_override') == committed['model_override']
            assert options.get('clarify_batch') is True
            assert 'api_text' not in options
            assert one_turn_reasoning(turn, {'effort': 'low'}) == {'enabled': True, 'effort': 'high'}
            assert cli_turn('admitted') == {}
            assert cli_turn('unrelated') == {}
            assert cli_turn(None) == {}
            turn._prepare_turn_message([])
            assert turn._ctx.message == expected_message
        assert cli_turn('admitted') is committed
    assert cli_turn('update-resume:admitted') == {}
    assert committed['api_text'] == 'ORIGINAL API TEXT MUST NOT REPLAY'


@pytest.mark.parametrize('message_id', ['owner', 'other', None])
def test_explicit_scope_and_accessor_require_identity(tmp_path, message_id):
    from gateway.session_update_pause_hook import bind_scope, current_scope, installed
    agent = SimpleNamespace()
    with bind_scope(tmp_path, eligible=True, resuming=True, message_id='owner') as scope:
        assert (current_scope(message_id) is scope) is (message_id == 'owner')
        assert current_scope() is None
        with installed(agent, scope, message_id=message_id) as active:
            assert active is (message_id == 'owner')
        with installed(agent, scope) as active:
            assert active is False


def test_explicit_missing_event_id_is_not_unbound_unit_scope(tmp_path):
    from gateway.session_update_pause_hook import bind_scope, current_scope, installed
    agent = SimpleNamespace()
    with bind_scope(tmp_path, eligible=True, message_id=None):
        assert current_scope(None) is None
        with installed(agent, message_id=None) as active:
            assert active is False
    with bind_scope(tmp_path, eligible=True):
        assert current_scope() is not None
        with installed(agent) as active:
            assert active is True
