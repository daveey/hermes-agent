"""Review admission is not implementation work; immutable re-handoffs keep author custody."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_managed_pin_policy as managed
from tests.hermes_cli import test_kanban_review_implementer_set as authors

board = managed.board
identity_board = authors.board
TIER = {'default_model': 'class:standard', 'default_provider': 'auto',
        'strong_model': 'class:deep', 'strong_provider': 'auto'}
ARTIFACT = {
    'published_pr': 'https://github.com/example/project/pull/12', 'pr_state': 'MERGED',
    'merge_commit': 'a' * 40, 'reviewed_sha': 'b' * 40,
}


def handoff(conn, tid, model, metadata):
    run = authors._run(conn, tid, receipt=model)
    conn.execute("UPDATE task_runs SET outcome='review_requested', metadata=? WHERE id=?",
                 (json.dumps(metadata), run))
    conn.commit()
    return run


def test_immutable_merged_rehandoff_does_not_consume_reviewer(identity_board):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.ASTRA, ARTIFACT)
        handoff(conn, tid, authors.OPUS, ARTIFACT)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {authors.ASTRA}


@pytest.mark.parametrize('delta', [
    {'merge_commit': 'c' * 40}, {'reviewed_sha': 'c' * 40},
    {'published_pr': 'https://github.com/example/project/pull/13'},
    {'pr_state': 'OPEN'}, {'merge_commit': None}, {'reviewed_sha': 'bad'},
    {'published_pr': ['https://github.com/example/project/pull/12']},
    {'published_pr': 'not-a-pr'},
])
def test_changed_or_unknown_artifact_keeps_author(identity_board, delta):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.ASTRA, ARTIFACT)
        handoff(conn, tid, authors.OPUS, {**ARTIFACT, **delta})
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {authors.ASTRA, authors.OPUS}


@pytest.mark.parametrize('metadata', [
    {**ARTIFACT, 'published_pr': 'not-a-pr'},
    {**ARTIFACT, 'reviewed_sha': 'bad'},
    {**ARTIFACT, 'merge_commit': None},
    {**ARTIFACT, 'pr_state': 'OPEN'},
    {}, [],
])
def test_repeated_unknown_identity_never_exempts_author(identity_board, metadata):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.ASTRA, metadata)
        handoff(conn, tid, authors.OPUS, metadata)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {authors.ASTRA, authors.OPUS}


def test_intervening_work_retains_next_handoff_author(identity_board):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.ASTRA, ARTIFACT)
        authors._run(conn, tid, receipt=authors.GROK)
        handoff(conn, tid, authors.OPUS, ARTIFACT)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {
            authors.ASTRA, authors.GROK, authors.OPUS}


def test_three_refusals_park_and_wake_stay_review(board, monkeypatch):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '0')
    conn, _ = board
    tid = managed._hand_off_for_review(conn, {'review_model': managed.FABLE})
    managed._receipts(conn, tid, route=('class:deep', None))
    for _ in range(3):
        dispatch_once(conn, spawn_fn=lambda *a: pytest.fail('unavailable reviewer spawned'), max_spawn=1)
    assert kb.get_task(conn, tid).status == 'triage'
    assert kb.schedule_task(conn, tid, reason='Review admission cooldown', clock_only=True,
                            awaiting={'kind': 'clock', 'why': 'review admission cooldown'})
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == 'review'


def test_review_admission_gave_up_does_not_escalate_implementation(board, monkeypatch):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '0')
    conn, _ = board
    tid = managed._hand_off_for_review(conn, {'review_model': managed.FABLE})
    managed._receipts(conn, tid, route=('class:deep', None))
    for _ in range(3):
        dispatch_once(conn, spawn_fn=lambda *a: pytest.fail('unavailable reviewer spawned'), max_spawn=1)
    task = kb.get_task(conn, tid)
    result = tier.resolve_model_tier(conn, task, lane='ready', cfg=TIER)
    assert result == ('class:standard', 'auto', 'default')


def test_worker_gave_up_still_escalates(board):
    conn, _ = board
    tid = kb.create_task(conn, title='Implementation', assignee='worker')
    run = authors._run(conn, tid, receipt=authors.OPUS)
    conn.execute("UPDATE task_runs SET outcome='gave_up' WHERE id=?", (run,))
    conn.commit()
    assert tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane='ready', cfg=TIER) == (
            'class:deep', 'auto', 'retry_after_run_gave_up')


def test_three_admission_failures_then_legacy_deep_wake_keeps_reviewer(board, monkeypatch):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '0')
    conn, _ = board
    tid = managed._hand_off_for_review(conn, ARTIFACT)
    original = review.resolve_review_model
    with monkeypatch.context() as refusal:
        def unavailable(*args):
            raise review.ReviewModelUnavailable('fixture: temporary reviewer wall')
        refusal.setattr(review, 'resolve_review_model', unavailable)
        for _ in range(3):
            dispatch_once(conn, spawn_fn=lambda *a: pytest.fail('refused reviewer spawned'), max_spawn=1)
    assert kb.get_task(conn, tid).status == 'triage'
    # Old installations lost the review lane and ran a new deep implementer.
    handoff(conn, tid, authors.OPUS, ARTIFACT)
    assert review.implementer_models(conn, kb.get_task(conn, tid)) == {'claude-sonnet-5-5'}
    assert original(conn, kb.get_task(conn, tid), tier.load_tier_config())[:2] == (authors.OPUS, 'anthropic')


@pytest.mark.parametrize('wake', ['unblock', 'timer'])
@pytest.mark.parametrize('source', ['triage', 'ready', 'running'])
def test_schedule_unblock_preserves_source_lane(board, source, wake):
    conn, _ = board
    tid = kb.create_task(conn, title='Wait', assignee='worker')
    conn.execute('UPDATE tasks SET status=? WHERE id=?', ('review' if source == 'running' else source, tid))
    conn.commit()
    if source == 'running':
        assert kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
    elif source == 'triage':
        kb._append_event(conn, tid, 'gave_up', {'resume_status': 'review'})
    assert kb.schedule_task(conn, tid, clock_only=True, awaiting={'kind': 'clock', 'why': 'cooldown'})
    if wake == 'unblock':
        assert kb.unblock_task(conn, tid)
    else:
        from hermes_cli.kanban_continuation import wake_due_continuations
        payload = [event.payload for event in kb.list_events(conn, tid) if event.kind == 'scheduled'][-1]
        assert wake_due_continuations(conn, now=payload['wake_at']) == [tid]
    assert kb.get_task(conn, tid).status == ('ready' if source == 'ready' else 'review')


@pytest.mark.parametrize('meta,error,expected', [
    ({'retry_status': 'review', 'trigger_outcome': 'spawn_failed'}, 'Review model resolution failed: no peer', True),
    ({'retry_status': 'ready', 'trigger_outcome': 'spawn_failed'}, 'Review model resolution failed: no peer', False),
    ({'retry_status': 'review', 'trigger_outcome': 'crashed'}, 'Review model resolution failed: no peer', False),
    ({'retry_status': 'review', 'trigger_outcome': 'spawn_failed'}, 'Worker crashed', False),
    ([], 'Review model resolution failed: no peer', False),
])
def test_historical_admission_failure_is_narrow(board, meta, error, expected):
    from hermes_cli.kanban_review_retry import admission_gave_up
    conn, _ = board
    assert admission_gave_up(conn, 'missing', -1, json.dumps(meta), error) is expected


@pytest.mark.parametrize('reason,expected', [('review_model_unavailable', True), ('other', False)])
def test_typed_admission_receipt_works_for_ready_lane_review(board, reason, expected):
    from hermes_cli.kanban_review_retry import admission_gave_up
    conn, _ = board
    tid = kb.create_task(conn, title='Independent review', assignee='worker')
    run = authors._run(conn, tid, receipt=authors.OPUS)
    kb._append_event(conn, tid, 'gave_up', {'admission_refusal': reason}, run_id=run)
    assert admission_gave_up(conn, tid, run, json.dumps({'retry_status': 'ready'}), 'refused') is expected
