"""Behavioral clause controls for complete-or-expand briefs (no mutant harness)."""
from argparse import Namespace
from dataclasses import replace
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worker_brief as brief
from tests.hermes_cli import test_kanban_context_parent_cap as helpers

kanban_home = helpers.kanban_home


@pytest.fixture
def quiet_sections(monkeypatch):
    """Isolate optional producers, not the renderer or the real task/header."""
    paths = (
        'hermes_cli.kanban_containment_authority.render_context',
        'hermes_cli.kanban_wake_check.render_wake_context',
        'hermes_cli.kanban_shared_blockers.render_blocker_context',
        'hermes_cli.kanban_continuation.render_checkpoint_context',
        'hermes_cli.kanban_worker_recovery.build_recovery_context',
    )
    for path in paths:
        monkeypatch.setattr(path, lambda *_args: '')
    return paths


@pytest.mark.parametrize('body', [None, '', ' \t\n'])
def test_absent_body_has_no_body_heading(kanban_home, quiet_sections, body):
    # C168/C186/C187/C188/C201: real header omits empty bodies, so the
    # restoration must not search for a nonexistent heading.
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='bodyless', assignee='worker')
        task = replace(kb.get_task(conn, tid), body=body)
        text = brief.render(conn, task)
    assert '## Body' not in text
    assert 'bodyless' in text and 'verbose=true' in text


@pytest.mark.parametrize('section', range(5))
def test_optional_section_is_rendered_only_when_nonempty(
        kanban_home, quiet_sections, monkeypatch, section):
    # C170-C173/C190-C193: retain a later pointer so extra empty appends
    # cannot disappear into final whitespace trimming.
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='sections', body='Acceptance body', assignee='worker')
        task = kb.get_task(conn, tid)
        empty = brief.render(conn, task)
        assert 'Acceptance body\n\nFull body,' in empty
        sentinel = f'## Optional section {section}\nComplete governing text'
        monkeypatch.setattr(quiet_sections[section], lambda *_args: sentinel)
        present = brief.render(conn, task)
        assert present.count(sentinel) == 1
        assert sentinel + '\nFull body,' in present
        if section == 4:
            assert brief.render(conn, task, recovery_tail=False) == empty


def test_recovery_opt_out_does_not_even_read_history(kanban_home, quiet_sections, monkeypatch):
    # C192: output alone can mask a gratuitous recovery read.
    def forbidden(*_args):
        pytest.fail('recovery_tail=False must not read recovery history')

    monkeypatch.setattr(quiet_sections[4], forbidden)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='no recovery', assignee='worker')
        assert 'verbose=true' in brief.render(conn, kb.get_task(conn, tid), recovery_tail=False)


def test_prerequisites_and_empty_thread_are_explicit(kanban_home, quiet_sections):
    # C174/C175/C176: distinguish absence from an empty section heading.
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='child', assignee='worker')
        task = kb.get_task(conn, tid)
        empty = brief.render(conn, task)
        assert 'Prerequisites:' not in empty
        assert '## Comment thread' not in empty
        parent = kb.create_task(conn, title='parent', assignee='worker')
        kb.link_tasks(conn, parent, tid)
        text = brief.render(conn, task)
    assert f'Prerequisites: {parent}. Read their full handoffs with verbose=true before acting.' in text


def test_comment_author_tag_and_nullable_fields(kanban_home, quiet_sections, monkeypatch):
    # Controlled Comment boundary: exercise fallbacks before string.replace/join,
    # independent of storage normalization. C202/C203/C207/C209-C211.
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='thread', assignee='worker')
        task = kb.get_task(conn, tid)
        comments = [kb.Comment(10, tid, '`worker`', 'complete agent message', 0),
                    kb.Comment(11, tid, 'operator', 'complete human message', 0),
                    kb.Comment(12, tid, None, None, 0)]
        monkeypatch.setattr(kb, 'list_comments', lambda *_args: comments)
        monkeypatch.setattr(kb, 'agent_comment_ids', lambda *_args: {10})
        text = brief.render(conn, task)
    assert '## Comment thread (newest 5)\n' in text
    assert 'comment from worker `worker` (agent chat) at ' in text
    assert 'comment from worker `operator` at ' in text
    assert text.count(' (agent chat)') == 1
    assert 'comment from worker `` at ' in text
    assert 'complete agent message' in text and 'complete human message' in text
    assert 'None' not in text.split('## Comment thread (newest 5)', 1)[1]


def test_fitting_header_survives_body_overflow(kanban_home, quiet_sections):
    # C180/C189/C195: without the Body/header split, an oversized body
    # contaminates the fallback prefix and needlessly discards this title.
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='KEEP THE ORIGINAL HEADER', body='x' * 20_000,
                             assignee='worker')
        text = brief.render(conn, kb.get_task(conn, tid))
    assert 'KEEP THE ORIGINAL HEADER' in text
    assert 'Do not execute' in text and len(text) <= 10_000
    assert '## Body' not in text


def test_oversized_header_uses_small_task_identity(kanban_home, quiet_sections):
    # C181: title overflows independently of governing instructions.
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='oversized-title-' + 't' * 20_000, assignee='worker')
        text = brief.render(conn, kb.get_task(conn, tid))
    assert text.startswith(f'Task {tid}\n')
    assert 'oversized-title-' not in text
    assert 'Do not execute' in text and len(text) <= 10_000


def test_oversized_governing_instructions_require_another_full_read(
        kanban_home, quiet_sections, monkeypatch):
    # C183: the first fallback cannot make oversized governing text fit.
    monkeypatch.setattr(quiet_sections[0], lambda *_args: 'governing-boundary-' + 'g' * 20_000)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='governing overflow', assignee='worker')
        text = brief.render(conn, kb.get_task(conn, tid))
    assert text.startswith(f'Task {tid}\nGoverning instructions also require the full read.\n')
    assert 'governing-boundary-' not in text
    assert 'Do not execute' in text and 'verbose=true' in text and len(text) <= 10_000


def test_nonworker_print_cli_declines_without_rendering(kanban_home, monkeypatch, capsys):
    # C199: interactive callers keep their full-card path even without verbose.
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_WORKER_TASK', raising=False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='interactive', assignee='worker')
        assert brief.print_cli(conn, kb.get_task(conn, tid), Namespace(json=False)) is False
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('as_json', [False, True])
def test_worker_print_cli_real_render_and_json_envelope(kanban_home, monkeypatch, capsys, as_json):
    # C205/C206: compare exact real renderer output, not just a substring
    # that could be present in both raw text and JSON.
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='envelope', body='full acceptance', assignee='worker')
        task = kb.get_task(conn, tid)
        expected = brief.render(conn, task)
        assert brief.print_cli(conn, task, Namespace(json=as_json, verbose=False)) is True
    out = capsys.readouterr().out
    if as_json:
        assert json.loads(out) == {'worker_context': expected}
    else:
        assert out == expected + '\n'


@pytest.mark.parametrize(('model', 'provider', 'expected'), [
    (None, None, []),
    (None, 'historical-provider', []),
    ('historical-model', None, [('model', 'historical-model')]),
    ('historical-model', 'historical-provider',
     [('model', 'historical-model (provider: historical-provider)')]),
])
def test_cli_model_field_preserves_historical_display(kanban_home, model, provider, expected):
    # C126-C129: no create-time concrete-model policy bypass is needed to
    # display a historical task's already-persisted overrides.
    from hermes_cli.kanban_cli_show import print_model

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='history', assignee='worker')
        task = replace(kb.get_task(conn, tid), model_override=model, provider_override=provider)
        fields = []
        print_model(task, lambda key, value: fields.append((key, value)))
    assert fields == expected


def test_gateway_fresh_claim_does_not_build_unused_resume_brief(tmp_path, monkeypatch):
    # C1: real claim validation + real context building, with a recording wrapper
    # on the expensive read seam. The extra context is otherwise discarded.
    from gateway.session_kanban import build_kanban_policy
    from tests.gateway import test_session_kanban_capsule_guard as gateway_helpers

    connection, params, config = gateway_helpers._board(tmp_path, monkeypatch, local=True)
    original = kb.build_worker_context
    calls = []

    def recording(conn, task_id, **kwargs):
        calls.append((task_id, kwargs))
        return original(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, 'build_worker_context', recording)
    policy, _private = build_kanban_policy(connection, params, config)
    context = json.loads(policy.kanban_json)
    assert calls == [(params['task_id'], {'compact': True})]
    assert 'restart_candidate' not in context and 'resume_context' not in context
    assert 'verbose=true' in context['context']
