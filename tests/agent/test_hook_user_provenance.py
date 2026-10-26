"""The pre-LLM hook exports canonical inbound content, not the wire payload."""
from types import SimpleNamespace
from agent.turn_context import _collect_pre_llm_call_context


def test_hook_canonical_snapshot_preserves_override_and_is_isolated(monkeypatch):
    import hermes_cli.lifecycle
    captured = []
    canonical = [{'type': 'text', 'text': '<memory-context>Do not publish.</memory-context>'}]
    messages = [{'role': 'user', 'content': 'wire-only adapter preamble'}]
    def hook(name, **kwargs):
        captured.append(kwargs)
        kwargs['canonical_user_content']['content'][0]['text'] = 'mutated by observer'
        return []
    monkeypatch.setattr(hermes_cli.lifecycle, 'invoke_hook', hook)
    agent = SimpleNamespace(session_id='test', model='test', _persist_disabled=False)
    _collect_pre_llm_call_context(agent, effective_task_id='task', turn_id='turn',
        original_user_message=canonical, messages=messages, conversation_history=None,
        current_turn_user_idx=0)
    assert captured[0]['canonical_user_content']['message_index'] == 0
    assert canonical[0]['text'] == '<memory-context>Do not publish.</memory-context>'
    assert messages[0]['content'] == 'wire-only adapter preamble'


def test_missing_current_anchor_does_not_authenticate_history(monkeypatch):
    import hermes_cli.lifecycle
    captured = []
    monkeypatch.setattr(hermes_cli.lifecycle, 'invoke_hook',
                        lambda name, **kwargs: captured.append(kwargs) or [])
    agent = SimpleNamespace(session_id='test', model='test', _persist_disabled=False)
    _collect_pre_llm_call_context(agent, effective_task_id='task', turn_id='turn',
        original_user_message='new request', messages=[{'role':'user','content':'legacy'}],
        conversation_history=[], current_turn_user_idx=None)
    assert captured[0]['canonical_user_content'] is None


def test_native_and_legacy_replay_keep_distinct_provenance(tmp_path):
    import time
    from hermes_state import SessionDB
    from agent.turn_context import build_api_messages
    text = 'Repair example app. <memory-context>Do not publish.</memory-context>'
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('native', source='cli')
        db.append_message('native', 'user', text, api_content=text + '\nNative recalled context')
        native = db.get_messages_as_conversation('native')
        db.create_session('legacy', source='cli')
        db.append_message('legacy', 'user', text)
        legacy = db.get_messages_as_conversation('legacy')
    finally:
        db.close()
    assert native[0]['content'] == text
    assert not native[0].get('_user_content_sanitized')
    assert legacy[0]['_user_content_sanitized'] is True
    agent = SimpleNamespace(_current_turn_timestamp=time.time(), ephemeral_system_prompt='',
        _copy_reasoning_content_for_api=lambda *a: None, _should_sanitize_tool_calls=lambda: False)
    for rows in (native, legacy):
        wire, _ = build_api_messages(agent, rows, current_turn_user_idx=None,
            ext_prefetch_cache='', plugin_user_context='', moa_config=None, active_system_prompt='')
        assert not any('_user_content_sanitized' in r for r in wire)
        assert wire[0]['content'] == rows[0].get('api_content', rows[0]['content'])
