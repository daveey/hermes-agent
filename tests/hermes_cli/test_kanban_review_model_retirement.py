"""Retired model diversity is not reviewer run/session independence."""
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_review_model as review
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_paced_reroute as paced
from tests.hermes_cli import test_kanban_review_model as helpers

board = helpers.board


def test_same_author_model_is_review_candidate(board):
    cfg = helpers.config(board, strong=helpers.OPUS, routes=[helpers.OPUS])
    with kbc.connect() as conn:
        task = helpers.task(conn, helpers.OPUS)
        assert tier.resolve_model_tier(conn, task, lane='review', cfg=cfg) == (
            helpers.OPUS, 'anthropic', 'review_unpinned')


def test_missing_author_model_does_not_deny_downstream_review(board):
    cfg = helpers.config(board, strong=helpers.OPUS, routes=[helpers.OPUS])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Independent exact-head review retirement fixture', assignee='default')
        task = kb.get_task(conn, tid)
        assert review.resolve_review_model(conn, task, cfg) == (helpers.OPUS, 'anthropic', 'review_unpinned')


def test_same_model_handoff_override_is_honored(board):
    cfg = helpers.config(board, strong=helpers.OPUS)
    with kbc.connect() as conn:
        task = helpers.task(conn, helpers.OPUS)
        kb.claim_task(conn, task.id)
        kb.request_review(conn, task.id, metadata={'review_model': helpers.OPUS, 'review_provider': 'anthropic'})
        assert review.resolve_review_model(conn, kb.get_task(conn, task.id), cfg) == (
            helpers.OPUS, 'anthropic', 'review_override')


def test_author_models_do_not_filter_candidates(board):
    cfg = helpers.config(board, strong=helpers.OPUS, routes=[helpers.OPUS])
    assert (helpers.OPUS, 'anthropic') in list(review.independent_routes({helpers.OPUS}, cfg, 'default'))


def test_same_model_pace_offer_allowed():
    review.validate_pace_offer(helpers.OPUS, {helpers.OPUS})


def test_missing_provenance_is_not_read_for_admission(board, monkeypatch):
    cfg = helpers.config(board, strong=helpers.OPUS)
    def unavailable(*_args):
        raise review.ReviewModelUnavailable('evicted author')
    monkeypatch.setattr(review, 'implementer_models', unavailable)
    with kbc.connect() as conn:
        task = helpers.task(conn)
        assert tier.apply_model_tier(conn, task, lane='review', cfg=cfg) == 'review_unpinned'


def test_same_author_model_respects_provider_wall(board, monkeypatch):
    from hermes_cli import kanban_provider_walls as walls
    cfg = helpers.config(board, strong=helpers.OPUS, routes=[helpers.OPUS])
    monkeypatch.setattr(walls, 'active_walls', lambda **_kw: {'anthropic': 9999999999})
    with kbc.connect() as conn:
        with pytest.raises(review.ReviewRouteDeferred):
            review.resolve_review_model(conn, helpers.task(conn, helpers.OPUS), cfg)


def test_paced_fallback_can_use_author_model(board, monkeypatch):
    cfg = helpers.config(board, strong=helpers.OPUS)
    monkeypatch.setattr(paced, '_probe', lambda *_a: (helpers.OPUS, 'anthropic', 'openai-codex'))
    with kbc.connect() as conn:
        task = helpers.task(conn, helpers.OPUS)
        assert paced.paced_route(conn, task, ('other', 'openai-codex', 'review_unpinned'), lane='review', cfg=cfg) == (
            helpers.OPUS, 'anthropic', 'review_unpinned+paced:openai-codex')
