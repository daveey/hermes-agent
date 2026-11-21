"""Independent reviewer probes for artifact custody across PR identities."""
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_review_model as review
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli import test_kanban_review_model as helpers
from tests.hermes_cli.test_kanban_review_model_cycles import MODELS, verdict
from tests.hermes_cli.test_kanban_review_retry_custody import ARTIFACT

board = helpers.board

@pytest.mark.parametrize('prior_range', [False, True])
def test_known_prior_head_in_new_pr_range_retains_author_without_excluding_route(board, prior_range):
    cfg = helpers.config(board, strong=MODELS[0], routes=MODELS)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='artifact cross-PR custody', assignee='default')
        def deliver(model, artifact):
            claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
            assert claimed
            kb._append_event(conn, tid, 'run_route', {'model': model, 'lane': 'ready'}, run_id=claimed.current_run_id)
            assert kb.request_review(conn, tid, metadata=artifact)
        first = dict(ARTIFACT)
        if prior_range:
            first['review_commit_range'] = {'base': '0'*40, 'head': 'b'*40, 'commits': ['b'*40]}
        deliver(MODELS[0], first)
        verdict(conn, tid, MODELS[1])
        deliver(MODELS[2], {**ARTIFACT, 'published_pr': 'https://github.com/example/project/pull/13',
            'reviewed_sha': 'd'*40, 'merge_commit': 'c'*40,
            'review_commit_range': {'base': '0'*40, 'head': 'd'*40, 'commits': ['b'*40, 'd'*40]}})
        claimed = kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
        authors = review.implementer_models(conn, claimed)
        chosen, _, _ = tier.resolve_model_tier(conn, claimed, lane='review', cfg=cfg)
        assert authors == {MODELS[0], MODELS[2]}
        assert chosen == MODELS[0] and chosen in authors

@pytest.mark.parametrize('with_ranges', [False, True])
def test_fifth_artifact_review_dispatch(board, monkeypatch, with_ranges):
    from hermes_cli import kanban_db_dispatch as dispatch
    helpers.config(board, strong=MODELS[0], routes=MODELS)
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '0')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='five artifact cycles', assignee='default')
        for index in range(5):
            model = MODELS[index % 4]
            sha = str(index + 1) * 40
            artifact = {'published_pr': ARTIFACT['published_pr'], 'reviewed_sha': sha}
            if with_ranges:
                artifact['review_commit_range'] = {'base': '0'*40, 'head': sha, 'commits': [sha]}
            claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
            assert claimed
            kb._append_event(conn, tid, 'run_route', {'model': model, 'lane': 'ready'}, run_id=claimed.current_run_id)
            assert kb.request_review(conn, tid, metadata=artifact)
            if index < 4:
                verdict(conn, tid, MODELS[(index + 1) % 4])
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args) or 4242)
        stored = kb.get_task(conn, tid)
        assert len(spawned) == 1
        assert stored.status == 'running'
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'model_tier']
        assert events[-1]['model'] == MODELS[0]
        assert events[-1]['review_independence'] == 'separate_execution'
        assert 'implementer_models' not in events[-1] and 'implementer_model' not in events[-1]
        # Explicit disjoint ranges narrow history; absent ranges conservatively retain every artifact author.
        assert review.implementer_models(conn, stored) == ({MODELS[0]} if with_ranges else set(MODELS))
