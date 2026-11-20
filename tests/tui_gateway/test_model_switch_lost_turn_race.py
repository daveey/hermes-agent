"""A pick whose unlocked ``running`` check lost to a turn admission is queued, not dropped or applied.

``config.set model`` and the ``/model`` slash mirror read ``session["running"]`` without a lock. The
agent's switch refuses to publish onto a turn admitted after its snapshot (``TurnAdmittedDuringSwitch``);
the gateway then stashes the pick for the next turn start through the busy path's own stash.
"""

import threading
import types

import pytest

from agent.agent_runtime_switch import TurnAdmittedDuringSwitch
from tui_gateway import server

TARGET = "claude-target"


def _refuse(*_args, **_kwargs):
    raise TurnAdmittedDuringSwitch("a turn started while the model switch resolved")


@pytest.fixture
def idle_session(monkeypatch):
    """An idle-looking session whose agent's switch loses the race to a turn."""
    result = types.SimpleNamespace(
        success=True, new_model=TARGET, target_provider="anthropic", base_url="", api_key="", api_mode="",
        warning_message="", error_message="", runtime_capabilities=None, usage_notice="")
    guard = types.SimpleNamespace(warning_message="")
    monkeypatch.setattr(server, "_current_model_runtime", lambda *_a, **_k: ("anthropic", "old", "", ""))
    monkeypatch.setattr(server, "_preflight_model_switch", lambda *_a, **_k: (result, guard, {}, {}))
    monkeypatch.setattr(server, "_merge_preflight_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_expensive_model_confirm", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_a, **_k: pytest.fail("restarted after refusal"))
    agent = types.SimpleNamespace(
        model="old", provider="anthropic", session_id="s", _one_turn_runtime=None, switch_model=_refuse)
    session = {"agent": agent, "session_key": "k", "history": [], "history_lock": threading.Lock(),
               "running": False, "slash_worker": None}
    server._sessions["sid"] = session
    try:
        yield session
    finally:
        server._sessions.pop("sid", None)


def test_config_set_queues_the_pick(idle_session):
    resp = server.handle_request({"id": "1", "method": "config.set",
                                  "params": {"session_id": "sid", "key": "model", "value": TARGET}})
    assert not resp.get("error"), resp
    assert resp["result"]["deferred"] is True and resp["result"]["value"] == TARGET
    assert idle_session["pending_model_switch"]["raw"] == TARGET
    assert "model_override" not in idle_session  # nothing was recorded as applied


def test_slash_mirror_queues_the_pick(idle_session):
    note = server._mirror_slash_side_effects("sid", idle_session, f"/model {TARGET}")
    assert "next turn" in note
    assert idle_session["pending_model_switch"]["raw"] == TARGET
    assert "model_override" not in idle_session


@pytest.mark.parametrize('options', [
    {'pin_session_override': False, 'persist_override': False, 'count_switch': False},
    {'pin_session_override': True, 'persist_override': None, 'count_switch': True},
])
def test_refused_switch_replays_original_intent(idle_session, monkeypatch, options):
    result = server._apply_model_switch('sid', idle_session, TARGET, confirm_expensive_model=True, **options)
    assert result['deferred'] is True
    pending = idle_session['pending_model_switch']
    assert {key: pending[key] for key in options} == options
    calls = []

    def applied(sid, session, raw, **kwargs):
        calls.append((sid, session, raw, kwargs))
        return {'confirm_required': False}

    monkeypatch.setattr(server, '_apply_model_switch', applied)
    server._apply_pending_model_switch('sid', idle_session)
    assert calls == [('sid', idle_session, TARGET, {'confirm_expensive_model': True, **options})]
    assert 'pending_model_switch' not in idle_session


def test_refused_moa_command_does_not_queue_a_permanent_pin(idle_session):
    response = server._cmd_moa('1', {'session_id': 'sid'}, idle_session, 'moa', 'fixture prompt')
    assert response['error']['code'] == 5030
    assert 'moa_one_shot_restore' not in idle_session
    assert 'pending_model_switch' not in idle_session
    assert 'model_override' not in idle_session
    assert idle_session['agent'].model == 'old'


def test_user_pick_during_moa_still_defers(idle_session):
    idle_session['moa_one_shot_restore'] = {'model': 'old'}
    result = server._apply_model_switch('sid', idle_session, TARGET, confirm_expensive_model=True)
    assert result['deferred'] is True
    assert idle_session['pending_model_switch']['raw'] == TARGET


def test_refused_second_moa_keeps_the_first_turn_restore(idle_session):
    restore = {'model': 'previous-model', 'provider': 'anthropic', 'override': None}
    idle_session['moa_one_shot_restore'] = restore
    idle_session['agent'].model = 'moa-preset'
    response = server._cmd_moa('1', {'session_id': 'sid'}, idle_session, 'moa', 'second prompt')
    assert response['error']['code'] == 5030
    assert idle_session['moa_one_shot_restore'] is restore
    assert 'pending_model_switch' not in idle_session


def test_deferred_reset_does_not_report_done(idle_session, monkeypatch):
    idle_session['model_override'] = {'model': 'old', 'provider': 'anthropic'}
    monkeypatch.setattr(server, '_config_model_target', lambda: (TARGET, 'anthropic'))
    monkeypatch.setattr(server, '_persist_live_session_runtime', lambda *a: pytest.fail('persisted before replay'))
    result = server._reset_session_model_override('sid', idle_session)
    assert idle_session['agent'].model == 'old'
    assert idle_session['pending_model_switch']['display_model'] == TARGET
    assert result.get('deferred') is True and result['warning'] == ''


def test_stash_warning_does_not_ack_an_older_pick(idle_session, monkeypatch):
    older = {'raw': 'older-pick', 'confirm_expensive_model': True}
    idle_session['pending_model_switch'] = older
    monkeypatch.setattr(server, '_pending_switch_selection_warning', lambda *a: 'costly')
    with pytest.raises(ValueError, match='pick claude-target again'):
        server._apply_model_switch('sid', idle_session, TARGET)
    assert idle_session['pending_model_switch'] is older
