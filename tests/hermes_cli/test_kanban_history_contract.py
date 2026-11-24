"""Historical inputs remain evidence; new route obligations remain no-write refused."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_model_neutral import validate_text
from tests.hermes_cli import test_kanban_kernel_history as fixtures

conn = fixtures.conn
clock = fixtures.clock

LEGACY = 'Independent primary Opus review required "quoted"\nrequire_model: true'


def decoded(body):
    """Historical lines decoded; each one's neutral active companion is asserted and skipped."""
    from hermes_cli.kanban_model_neutral import neutral_line
    prefix = 'Historical run instruction (superseded and not active): '
    out, lines = [], iter(body.splitlines())
    for line in lines:
        if line.startswith(prefix):
            line = json.loads(line[len(prefix):])
            if neutral_line(line) is not None:
                assert next(lines) == neutral_line(line)
        out.append(line)
    return '\n'.join(out)


def source(conn):
    tid = kb.create_task(conn, title='ordinary source', body='original approved scope')
    conn.execute('UPDATE tasks SET title=? WHERE id=?', (LEGACY, tid))
    return tid


def preserved(conn, tid, child):
    assert kb.get_task(conn, tid).title == LEGACY
    assert kb.get_task(conn, tid).body == 'original approved scope'
    assert LEGACY in decoded(child.body)
    assert child.model_override is None and child.provider_override is None
    validate_text(child.title, child.body)
    for directive in ('Use only grok-4.6 / xai-oauth.', 'require_model: true'):
        before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        with pytest.raises(ValueError, match='model-neutral'):
            kb.create_task(conn, title='active policy', body=child.body + '\n' + directive)
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before


def test_missing_parent_history(conn):
    from hermes_cli.kanban_db_graph import missing_parent_repair
    (Path(os.environ['HERMES_HOME']) / 'config.yaml').write_text('platform_toolsets:\n  cli: [terminal, file, kanban]\n')
    tid = source(conn)
    child = kb.get_task(conn, missing_parent_repair(conn, tid, ['t_0123456789abcdef']))
    preserved(conn, tid, child)
    assert 'Do not unlink a required dependency' in child.body


def test_repark_history(conn, monkeypatch):
    from hermes_cli import kanban_awaiting as awaiting
    monkeypatch.setattr(awaiting, '_supervisor_profile', lambda task: None)
    tid = source(conn)
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    record = {'premise': 'fixture-incident', 'kind': 'external', 'why': LEGACY}
    with kb.write_txn(conn):
        owner = awaiting.escalate(conn, tid, record=record, reason=LEGACY)
    child = kb.get_task(conn, owner)
    preserved(conn, tid, child)
    assert kb.get_task(conn, tid).status == 'todo'
    assert owner in kb.parent_ids(conn, tid)


def test_systemic_crash_history(conn):
    from hermes_cli.kanban_systemic_crash import ensure_fix_card
    tid = source(conn)
    child = kb.get_task(conn, ensure_fix_card(conn, LEGACY, [tid], LEGACY, 'default'))
    preserved(conn, tid, child)
    assert 'Do not re-queue them by hand' in child.body
    assert ensure_fix_card(conn, LEGACY, [tid], LEGACY, 'default') == child.id


def test_capability_owner_history_not_active_action(conn):
    from hermes_cli.kanban_capability_routing import local_owner_text
    tid = source(conn)
    task = SimpleNamespace(id=tid, title=LEGACY)
    title, body = local_owner_text(task, 'ssh:fixture', self_gw='holder', from_gw='origin', reason='Inspect the service')
    child = kb.get_task(conn, kb.create_task(conn, title=title, body=body))
    preserved(conn, tid, child)
    assert 'Inspect the service' in child.body
    title, body = local_owner_text(task, 'ssh:fixture', self_gw='holder', from_gw='origin', reason='Use only grok-4.6')
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title=title, body=body)


@pytest.mark.parametrize('text', [
    'Use only grok-4.6 / xai-oauth.',
    'The reviewer must use claude-opus-5-5.',
    'Required exact-head independent review with anthropic/claude-opus-5-5.',
    'grok-4.6 review required.',
    'gpt-6-astra is mandatory.',
    'Pin the reviewer to gpt-6-luna.',
    'No fallback for claude-opus-5-5.',
    'Pinned route xai-oauth/grok-4.6 is unavailable; use only gpt-6-luna.',
    'Pinned route xai-oauth/grok-4.6 is unavailable: use only gpt-6-luna.',
])
def test_active_obligation_no_write(conn, text):
    before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='active policy', body=text)
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before


@pytest.mark.parametrize('text', [
    'Pinned route xai-oauth/grok-4.6 is unavailable: no available credential',
    'Bug: gpt-6-astra tool calls only return empty arguments after compaction',
    'Prior claude-opus-5-5 run required 12 tool calls; required CI is green.',
])
def test_diagnostics_not_routing_policy(conn, text):
    assert kb.create_task(conn, title='Investigate observed failure', body=text)
