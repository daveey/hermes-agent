"""Review structural refusal belongs before claim/run allocation, on both lanes."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_managed_pin_policy as helpers

board = helpers.board


def unknown_author(conn, lane):
    tid = helpers._hand_off_for_review(conn, {})
    helpers._receipts(conn, tid, route=('class:deep', None))
    if lane == 'ready':
        conn.execute("UPDATE tasks SET status='ready', skills=? WHERE id=?", ('["sdlc-review"]', tid))
        conn.commit()
    return tid


def result():
    return SimpleNamespace(clock_held={}, respawn_guarded=[], auto_blocked=[])


@pytest.mark.parametrize('lane', ['review', 'ready'])
def test_dry_review_refusal_is_read_only(board, lane):
    from hermes_cli.kanban_review_admission import hold
    conn, _ = board
    tid = unknown_author(conn, lane)
    before = list(conn.iterdump())
    assert hold(conn, tid, result(), lane=lane, dry_run=True)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize('lane', ['review', 'ready'])
def test_refusal_cooldown_and_foreign_home_never_allocate(board, monkeypatch, lane):
    from hermes_cli.kanban_review_admission import hold
    from hermes_cli import kanban_wake_check
    conn, _ = board
    tid = unknown_author(conn, lane)
    before_runs = [tuple(r) for r in conn.execute('SELECT * FROM task_runs')]
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '300')
    assert hold(conn, tid, result(), lane=lane, dry_run=False)
    before = list(conn.iterdump())
    for _ in range(4):
        assert hold(conn, tid, result(), lane=lane, dry_run=False)
    assert list(conn.iterdump()) == before
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '0')
    monkeypatch.setattr(kanban_wake_check, 'foreign_copy', lambda *a: True)
    assert hold(conn, tid, result(), lane=lane, dry_run=False)
    assert list(conn.iterdump()) == before
    assert [tuple(r) for r in conn.execute('SELECT * FROM task_runs')] == before_runs


@pytest.mark.parametrize('lane', ['review', 'ready'])
@pytest.mark.parametrize('blocked', [False, True])
def test_known_author_actual_dispatch_still_obeys_candidate_walls(board, monkeypatch, lane, blocked):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    from hermes_cli import kanban_provider_walls as walls
    from hermes_cli import kanban_llm_pacing_dispatch as pacing
    conn, _ = board
    tid = helpers._hand_off_for_review(conn, {})
    if lane == 'ready':
        source = tid
        # Existing dispatcher-created source receipt, not a declaration or pin.
        assert kb.complete_task(conn, source, summary='Fixture implementation complete')
        tid = kb.create_task(conn, title='Independent review of Browser build',
                             assignee='worker', creator_task_id=source,
                             skills=['sdlc-review'])
        from hermes_cli.kanban_review_model import implementer_models
        assert implementer_models(conn, kb.get_task(conn, tid)) == {'claude-sonnet-5-5'}
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *a: None)
    monkeypatch.setattr(pacing, 'held_pools', lambda *a: {'anthropic': 9999999999, 'openai-codex': 9999999999} if blocked else {})
    before_runs = conn.execute('SELECT count(*) FROM task_runs').fetchone()[0]
    seen = []
    outcome = dispatch_once(conn, spawn_fn=lambda task, *a: seen.append(task.model_override), max_spawn=1)
    assert len(seen) == (0 if blocked else 1)
    assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == before_runs + (0 if blocked else 1)
    if blocked:
        assert outcome.clock_held[tid][0] == 'review_provider_wall'
    else:
        assert seen[0] != 'claude-sonnet-5-5'  # authenticated actual author remains excluded
    task = kb.get_task(conn, tid)
    assert task is not None and task.model_override is None and task.provider_override is None
