"""Independent review: active restrictions at common mutation boundaries."""
import pytest
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_managed_pin_policy as support

board = support.board

@pytest.mark.parametrize('body', [
    'Must use opus',
    'Must use `opus`',
    'Must use **opus**',
    'Must use anthropic',
    'Use only the anthropic provider.',
    '<!-- implementer-models: sonnet --> Must use opus',
    'Must use *opus*',
    'Must use __opus__',
    'Must use ``claude-opus-5-5``',
    'Must use **anthropic**',
    'Use only the openai-codex provider.',
    'anthropic provider is required.',
    '<!-- implementer-models: opus --> <!-- implementer-models: sonnet --> Must use `opus`',
    'Do not restrict to anthropic; Must use `opus`',
])
@pytest.mark.parametrize('entry', ['create', 'edit'])
def test_active_restriction_refuses_without_write(board, body, entry):
    conn, _ = board
    tid = kb.create_task(conn, title='Review fixture', body='Neutral work', assignee='worker')
    before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError, match='model-neutral'):
        if entry == 'create':
            kb.create_task(conn, title='New work', body=body, assignee='worker')
        else:
            kb.edit_task(conn, tid, body=body)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before
    assert kb.get_task(conn, tid).body == 'Neutral work'


@pytest.mark.parametrize('body', [
    '<!-- implementer-models: sonnet -->',
    '<!-- implementer-models: opus --> Preserve independent execution.',
    'Implement the anthropic provider. Required CI must pass.',
    'The xai-oauth route only emits declared keys. Confirm the mechanism first.',
    'The wall file now lists openai-codex only. Anthropic serves.',
    'Gemini only if its key is subscription-funded (verify funding; metered API = skip and say so).',
    'The only rule covers xAI only (the runbook).',
    'Checked: routing_inventory.py executed; xai-oauth only on the laptop.',
    'This is the second time: the xAI case was fixed for xAI only.',
    'Scenario: config.yaml model.default; choose Anthropic; click sign in; then dismiss.',
    'Must use anthropic API headers for the authentication probe.',
    'Use only the anthropic provider fixture for this unit test.',
    'The completed review used `opus`; required CI passed.',
    'No longer requires **opus**.',
    'Do not restrict to anthropic.',
    'This review does not require anthropic.',
    'Historical run instruction (superseded and not active): "Must use **opus**"',
    'Historical run instruction (superseded and not active): "Use only the anthropic provider."',
])
@pytest.mark.parametrize('entry', ['create', 'edit'])
def test_nonmandate_text_retained_unchanged(board, body, entry):
    conn, _ = board
    tid = kb.create_task(conn, title='Neutral fixture', body='Neutral work', assignee='worker')
    if entry == 'create':
        tid = kb.create_task(conn, title='Provider implementation', body=body, assignee='worker')
    else:
        assert kb.edit_task(conn, tid, body=body)
    assert kb.get_task(conn, tid).body == body

