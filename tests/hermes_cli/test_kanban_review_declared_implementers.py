"""A downstream review card may declare its implementer models in its body. [daveey]

2026-11-19: since 3249911ef27 a downstream review fails closed without implementer evidence, and that evidence came
only from linked source cards. A change written in a Claude Code session (no Kanban card) could therefore never be
reviewed: review card t_01a1f27c393d49da for PR #3633 gave up twice with "Downstream review implementer evidence is
unavailable". ``hermes-core-wt ship`` now writes ``<!-- implementer-models: ... -->`` from the commits'
Co-Authored-By trailers.

Entry paths: ``declared_implementers`` (pure), ``implementer_models`` and ``resolve_review_model`` on a real SQLite
board (the fixtures of test_kanban_review_implementer_set).
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_review_implementer_set as base
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- pytest fixture

clock = base.clock
OPUS, ASTRA = base.OPUS, base.ASTRA
TITLE = 'Independent exact-head review PR #1 @abc (core-wt ship review required)'


@pytest.mark.parametrize('body, expected', [
    ('<!-- implementer-models: claude-opus-5-5 -->', {'claude-opus-5-5'}),
    ('intro\n<!-- implementer-models: claude-fable-5-2, claude-opus-5-5 -->\nmore', {'claude-fable-5-2', 'claude-opus-5-5'}),
    ('<!-- implementer-models: a-model -->\n<!-- implementer-models: b-model -->', {'a-model', 'b-model'}),
    ('', set()),
    (None, set()),
    ('implementer-models: claude-opus-5-5', set()),                      # not the marker line
    ('  <!-- implementer-models: claude-opus-5-5 -->', set()),             # must start the line
    ('<!-- implementer-models: class:deep -->', set()),                    # a class is no implementer
    ('<!-- implementer-models: claude-opus-5-5, Bad Name -->', set()),     # one bad entry voids the declaration
    ('<!-- implementer-models:  -->', set()),
    ('<!-- implementer-models: a-model -->\n<!-- implementer-models: Bad Name -->', set()),  # a later bad line too
    ('<!-- implementer-models: claude-opus x -->', set()),                                  # the whole entry must match
])
def test_declared_implementers(body, expected, monkeypatch):
    monkeypatch.setattr(review, 'registry_refusal', lambda *a: None)  # every well-formed name is registered here
    assert review.declared_implementers(body) == expected


def test_a_name_the_routing_registry_does_not_know_voids_the_declaration(monkeypatch):
    """Review 5971146857: ``claude-code`` excludes no real route, so it must not count as implementer evidence."""
    known = {'claude-opus-5-5', 'claude-fable-5-2'}
    monkeypatch.setattr(review, 'registry_refusal', lambda model, provider, assignee: None if model in known else 'no')
    assert review.declared_implementers('<!-- implementer-models: claude-opus-5-5 -->') == {'claude-opus-5-5'}
    assert review.declared_implementers('<!-- implementer-models: claude-code -->') == set()
    assert review.declared_implementers('<!-- implementer-models: claude-opus-5-5, claude-code -->') == set()


def _review(conn, body):
    tid = kb.create_task(conn, title=TITLE, assignee='a', body=body)
    conn.commit()
    return kb.get_task(conn, tid)


def test_a_declared_review_has_implementer_evidence(board):
    with kbc.connect_closing() as conn:
        task = _review(conn, f'Review it.\n<!-- implementer-models: {OPUS} -->\n')
        assert review.implementer_models(conn, task) == {OPUS}


def test_an_undeclared_review_with_no_source_still_fails_closed(board):
    with kbc.connect_closing() as conn:
        task = _review(conn, 'Review it.')
        assert review.implementer_models(conn, task) == set()
        with pytest.raises(review.ReviewModelUnavailable, match='Downstream review implementer evidence'):
            review.resolve_review_model(conn, task, {})


def test_declared_and_linked_implementers_are_both_excluded(board):
    with kbc.connect_closing() as conn:
        source = base._card(conn, ASTRA)
        task = _review(conn, f'<!-- implementer-models: {OPUS} -->')
        kb.link_tasks(conn, task.id, source)
        conn.commit()
        assert review.implementer_models(conn, kb.get_task(conn, task.id)) == {OPUS, ASTRA}


def test_a_declaration_on_a_card_that_is_not_a_review_is_ignored(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='implement it', assignee='a',
                             body=f'<!-- implementer-models: {OPUS} -->')
        conn.commit()
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == set()


def test_the_reviewer_is_never_a_declared_implementer(board):
    """Through ``resolve_review_model`` with a real routing config (deep routes opus and fable, strong opus): the
    declared implementer opus is excluded, so fable reviews."""
    from tests.hermes_cli import test_kanban_review_model as routing
    cfg = routing.config(board)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=TITLE, assignee='default', body=f'<!-- implementer-models: {routing.OPUS} -->')
        conn.commit()
        task = kb.get_task(conn, tid)
        assert review.implementer_models(conn, task) == {routing.OPUS}
        assert review.resolve_review_model(conn, task, cfg)[0] == routing.FABLE
