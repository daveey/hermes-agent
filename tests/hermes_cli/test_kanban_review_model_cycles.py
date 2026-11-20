"""Current-head authorship survives retries without exhausting historical reviewers."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_review_implementer_set as provenance
from tests.hermes_cli import test_kanban_review_model as helpers

board = helpers.board
MODELS = [helpers.OPUS, helpers.FABLE, helpers.SONNET, "claude-review-four"]


def run(conn, tid, model, lane="ready"):
    rid = provenance._run(conn, tid, model=model, receipt=model, lane=lane)
    if lane == "review":
        conn.execute("UPDATE task_runs SET outcome='changes_requested' WHERE id=?", (rid,))
    return rid


def test_fifth_review_reuses_older_models_not_current_author(board):
    cfg = helpers.config(board, strong=MODELS[0], routes=MODELS)
    with kbc.connect() as conn:
        task = helpers.task(conn, MODELS[0])
        for index, model in enumerate(MODELS):
            run(conn, task.id, model)
            run(conn, task.id, MODELS[(index + 1) % len(MODELS)], "review")
        run(conn, task.id, MODELS[-1])
        model, _, _ = tier.resolve_model_tier(conn, kb.get_task(conn, task.id), lane="review", cfg=cfg)
        assert model in MODELS and model != MODELS[-1]
        assert review.implementer_models(conn, kb.get_task(conn, task.id)) == {MODELS[-1]}


@pytest.mark.parametrize("current", MODELS)
def test_current_epoch_excludes_all_authors_even_across_review_retries(board, current):
    cfg = helpers.config(board, strong=current, routes=MODELS)
    with kbc.connect() as conn:
        task = helpers.task(conn, MODELS[0])
        for model in MODELS:
            run(conn, task.id, model)
            run(conn, task.id, model, "review")
        other = MODELS[(MODELS.index(current) + 1) % len(MODELS)]
        run(conn, task.id, current)
        run(conn, task.id, other)
        for _ in range(2):
            run(conn, task.id, MODELS[0], "review")
        authors = review.implementer_models(conn, kb.get_task(conn, task.id))
        assert authors == {current, other}
        model, _, _ = tier.resolve_model_tier(conn, kb.get_task(conn, task.id), lane="review", cfg=cfg)
        assert model not in authors


@pytest.mark.parametrize("outcome", ["spawn_failed", "scheduled", "crashed"])
def test_refused_spawn_is_not_a_review_boundary(board, outcome):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="epoch fixture", assignee="default"))
        run(conn, task.id, MODELS[0])
        review_id = run(conn, task.id, MODELS[1], "review")
        conn.execute("DELETE FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result'",
                     (task.id, review_id))
        conn.execute("UPDATE task_runs SET outcome=? WHERE id=?", (outcome, review_id))
        if outcome != "spawn_failed":
            kb._append_event(conn, task.id, "spawned", {"pid": 123}, run_id=review_id)
        run(conn, task.id, MODELS[2])
        assert review.implementer_models(conn, kb.get_task(conn, task.id)) == {MODELS[0], MODELS[2]}


@pytest.mark.parametrize("pin", [None, MODELS[0]])
def test_missing_receipt_in_mixed_epoch_retains_earlier_authors(board, pin):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="mixed epoch", assignee="default",
                                              model_override=pin, provider_override="anthropic" if pin else None))
        run(conn, task.id, MODELS[0])
        run(conn, task.id, MODELS[1], "review")
        run(conn, task.id, None)
        run(conn, task.id, MODELS[2])
        assert review.implementer_models(conn, kb.get_task(conn, task.id)) == {MODELS[0], MODELS[2]}


def test_missing_current_receipt_keeps_previous_author_evidence(board):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="unknown author", assignee="default"))
        run(conn, task.id, MODELS[0])
        run(conn, task.id, MODELS[1], "review")
        unknown = run(conn, task.id, None)
        conn.execute("UPDATE task_runs SET outcome='review_requested' WHERE id=?", (unknown,))
        assert review.implementer_models(conn, kb.get_task(conn, task.id)) == {MODELS[0]}


@pytest.mark.parametrize("how", ["schedule", "block", "crash", "verdict"])
def test_real_reviewer_lifecycle_only_verdict_releases_authors(board, how):
    cfg = helpers.config(board, strong=MODELS[0], routes=MODELS)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="lifecycle epoch", assignee="default")

        def implement(model):
            claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
            assert claimed is not None
            kb._append_event(conn, tid, "run_route", {"model": model, "lane": "ready"},
                             run_id=claimed.current_run_id)
            assert kb.request_review(conn, tid)

        implement(MODELS[0])
        claimed = kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
        assert claimed is not None
        rid = claimed.current_run_id
        kb._append_event(conn, tid, "model_tier", {"model": MODELS[1], "lane": "review"}, run_id=rid)
        kb._append_event(conn, tid, "spawned", {"pid": 123}, run_id=rid)
        if how == "verdict":
            assert kb.request_changes(conn, tid, reason="rework")[0]
        elif how == "crash":
            provenance._end_run(conn, rid)
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL WHERE id=?", (tid,))
        else:
            if how == "schedule":
                from hermes_cli.kanban_db_schedule import schedule_task
                assert schedule_task(conn, tid, reason="CI", clock_only=True)
            else:
                assert kb.block_task(conn, tid, reason="wall", kind="transient", clock_only=True)
            assert kb.unblock_task(conn, tid)
            # Main resumes the review phase after a parked review; model the later re-open to ready.
            conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (tid,))
        implement(MODELS[2])
        authors = review.implementer_models(conn, kb.get_task(conn, tid))
        assert authors == ({MODELS[2]} if how == "verdict" else {MODELS[0], MODELS[2]})
        model, _, _ = tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane="review", cfg=cfg)
        assert model not in authors


def implement(conn, tid, model):
    claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert claimed is not None
    kb._append_event(conn, tid, "run_route", {"model": model, "provider": "anthropic", "lane": "ready"},
                     run_id=claimed.current_run_id)
    assert kb.request_review(conn, tid)


def verdict(conn, tid, model):
    claimed = kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
    assert claimed is not None
    kb._append_event(conn, tid, "model_tier", {"model": model, "lane": "review"},
                     run_id=claimed.current_run_id)
    assert kb.request_changes(conn, tid, reason="rework")[0]


@pytest.mark.parametrize("refused", [False, True])
def test_claimed_review_without_route_is_not_an_unknown_author(board, refused):
    cfg = helpers.config(board, strong=MODELS[0], routes=MODELS)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="claimed review epoch", assignee="default")
        implement(conn, tid, MODELS[0])
        verdict(conn, tid, MODELS[1])
        implement(conn, tid, MODELS[2])
        claimed = kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
        assert claimed is not None
        if refused:
            # No model_tier exists when admission refuses before resolution.
            conn.execute("UPDATE task_runs SET outcome='spawn_failed' WHERE id=?", (claimed.current_run_id,))
        assert review.implementer_models(conn, claimed) == {MODELS[2]}
        model, _, _ = tier.resolve_model_tier(conn, claimed, lane="review", cfg=cfg)
        assert model == MODELS[0]


def test_dispatch_fifth_review_after_four_real_verdict_cycles(board, monkeypatch):
    helpers.config(board, strong=MODELS[0], routes=MODELS)
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    spawned = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="dispatch review epoch", assignee="default")
        for index, model in enumerate(MODELS):
            implement(conn, tid, model)
            verdict(conn, tid, MODELS[(index + 1) % len(MODELS)])
        implement(conn, tid, MODELS[-1])
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args) or 4242)
        assert len(spawned) == 1
        assert result.auto_blocked == []
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == "model_tier"]
        assert events[-1]["model"] == MODELS[0]
        assert events[-1]["implementer_models"] == [MODELS[-1]]


@pytest.mark.parametrize("merged", [True, False])
@pytest.mark.parametrize("middle", ["continue", "crash", "route_only", "other_artifact"])
def test_artifact_authors_survive_nonhandoff_rework(board, middle, merged):
    from hermes_cli.kanban_continuation import continue_task
    from tests.hermes_cli.test_kanban_review_retry_custody import ARTIFACT
    artifact = ARTIFACT if merged else {key: value for key, value in ARTIFACT.items()
                                      if key not in ("pr_state", "merge_commit")}
    cfg = helpers.config(board, strong=MODELS[0], routes=MODELS)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="artifact custody", assignee="default")

        def deliver(model, artifact):
            claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
            assert claimed
            kb._append_event(conn, tid, "run_route", {"model": model, "lane": "ready"},
                             run_id=claimed.current_run_id)
            assert kb.request_review(conn, tid, metadata=artifact)

        deliver(MODELS[0], artifact)
        verdict(conn, tid, MODELS[1])
        if middle == "other_artifact":
            deliver(MODELS[2], {**ARTIFACT, "published_pr": "https://github.com/example/project/pull/13",
                                "merge_commit": "c" * 40, "reviewed_sha": "d" * 40})
            verdict(conn, tid, MODELS[1])
        else:
            claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
            assert claimed
            kb._append_event(conn, tid, "run_route", {"model": MODELS[2], "lane": "ready"},
                             run_id=claimed.current_run_id)
            if middle == "continue":
                assert continue_task(conn, tid, checkpoint="Rework remains")["ok"]
            else:
                if middle == "crash":
                    kb._append_event(conn, tid, "spawned", {"pid": 123}, run_id=claimed.current_run_id)
                provenance._end_run(conn, claimed.current_run_id)
                conn.execute("UPDATE tasks SET status='triage', current_run_id=NULL, claim_lock=NULL WHERE id=?", (tid,))
                assert kb.promote_task(conn, tid, actor="operator")[0]
        deliver(MODELS[3], artifact)
        claimed = kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
        excluded = review.implementer_models(conn, claimed)
        assert MODELS[0] in excluded
        chosen, _, _ = tier.resolve_model_tier(conn, claimed, lane="review", cfg=cfg)
        assert chosen not in excluded


def test_provider_wall_does_not_become_structural_exhaustion(board, monkeypatch):
    cfg = helpers.config(board)
    from hermes_cli import kanban_provider_walls as walls
    monkeypatch.setattr(walls, "active_walls", lambda **kwargs: {"anthropic": 99999999999})
    with kbc.connect() as conn:
        with pytest.raises(review.ReviewModelUnavailable) as raised:
            review.resolve_review_model(conn, helpers.task(conn), cfg)
        infrastructure, force_trip, _ = review.admission_refusal(conn, "absent", raised.value)
        assert infrastructure and not force_trip


def test_structural_exhaustion_blocks_once_without_spawn_retry_loop(board, monkeypatch):
    helpers.config(board, strong=helpers.OPUS, routes=[helpers.OPUS])
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    spawned = []
    with kbc.connect() as conn:
        task = helpers.task(conn, helpers.OPUS)
        kb.claim_task(conn, task.id)
        kb.request_review(conn, task.id)
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args))
        stored = kb.get_task(conn, task.id)
        # Canonical non-human infrastructure block is sticky triage, never a David ask.
        assert stored.status == "triage"
        assert result.auto_blocked == [task.id]
        assert stored.human_request is None
        assert helpers.OPUS in stored.last_failure_error
        events = kb.list_events(conn, task.id)
        gave_up = [e.payload for e in events if e.kind == "gave_up"]
        assert len(gave_up) == 1
        assert gave_up[0]["infrastructure"] is True
        assert gave_up[0]["admission_refusal"] == "review_models_exhausted"
        assert gave_up[0]["sticky"] is True
        runs = len(kb.list_runs(conn, task.id))
        for _ in range(4):
            dispatch.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args))
        assert len(kb.list_runs(conn, task.id)) == runs
        assert len([e for e in kb.list_events(conn, task.id) if e.kind == "gave_up"]) == 1
        assert spawned == []


def test_downstream_review_keeps_full_history_of_source_authors(board):
    with kbc.connect() as conn:
        src = kb.get_task(conn, kb.create_task(conn, title="source work", assignee="default"))
        run(conn, src.id, MODELS[0])
        run(conn, src.id, MODELS[1], "review")
        run(conn, src.id, MODELS[2])
        down = kb.get_task(conn, kb.create_task(conn, title="Independent exact-head review of PR",
                                                assignee="default", creator_task_id=src.id))
        # The PR diff holds both epochs' commits; only the card's own lane is bounded.
        assert review.implementer_models(conn, down) == {MODELS[0], MODELS[2]}
        assert review.implementer_models(conn, kb.get_task(conn, src.id)) == {MODELS[2]}


def test_completed_review_outcome_is_an_epoch_boundary(board):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="completed verdict", assignee="default"))
        run(conn, task.id, MODELS[0])
        verdict_run = run(conn, task.id, MODELS[1], "review")
        conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (verdict_run,))
        run(conn, task.id, MODELS[2])
        assert review.implementer_models(conn, kb.get_task(conn, task.id)) == {MODELS[2]}


def _walled(monkeypatch):
    from hermes_cli import kanban_provider_walls as walls
    monkeypatch.setattr(walls, "active_walls", lambda **kwargs: {"anthropic": 99999999999})


def test_walled_override_with_no_other_candidate_is_retryable_not_exhausted(board, monkeypatch):
    cfg = helpers.config(board, strong=helpers.OPUS, routes=[helpers.OPUS])
    _walled(monkeypatch)
    with kbc.connect() as conn:
        task = helpers.task(conn, helpers.OPUS)
        kb.claim_task(conn, task.id)
        assert kb.request_review(conn, task.id, metadata={"review_model": helpers.FABLE})
        with pytest.raises(review.ReviewModelUnavailable) as raised:
            review.resolve_review_model(conn, kb.get_task(conn, task.id), cfg)
        assert not isinstance(raised.value, review.ReviewModelsExhausted)


def test_walled_standard_candidate_is_retryable_not_exhausted(board, monkeypatch):
    import json
    cfg = helpers.config(board, strong=helpers.OPUS, routes=[helpers.OPUS])
    path = board.joinpath("config.yaml")
    data = json.loads(path.read_text(encoding="utf-8"))
    deep = data["model_routing"]["routes"]["deep"][0]
    data["model_routing"]["routes"]["standard"] = [{**deep, "model": helpers.FABLE}]
    path.write_text(json.dumps(data), encoding="utf-8")
    _walled(monkeypatch)
    with kbc.connect() as conn:
        task = helpers.task(conn, helpers.OPUS)
        kb.claim_task(conn, task.id)
        assert kb.request_review(conn, task.id)
        with pytest.raises(review.ReviewModelUnavailable) as raised:
            review.resolve_review_model(conn, kb.get_task(conn, task.id), cfg)
        assert not isinstance(raised.value, review.ReviewModelsExhausted)
