"""Real local Board: a stop reviewed by the dispatcher's own maintenance after the configured delay.

Opt in exactly as ``test_kanban_spend_allowance_board`` (B2_M1_LOCAL_BOARD_URL). The judge is stubbed (no provider
request); the Board operation, sync, pickup and claim are real.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import test_kanban_spend_allowance_board as base

composed = base.composed
QUOTE, uid = base.QUOTE, base.uid


def _stop_again(home, task, summary):
    """A second real scope stop of the reviewed card (one debit of its allowance), pending and unreviewed."""
    run = uid()
    granted = home.hub.leases(base.BOARD, {"action": "grant", "task": task, "run_uid": run, "ttl_s": 900})
    assert granted["status"] == "applied", granted
    retained = home.hub.ops(base.BOARD, [{
        "client_op_id": uid(), "op": "retain", "task": task, "run_uid": run, "event_uid": uid(),
        "expected_episode": None, "payload": {
            "version": 1, "summary": summary, "reason": "scope_stopped", "next_step": "Owner decides",
            "release_condition": "Scoped decision", "evidence": ["isolated composition fixture"]}}])
    assert retained["results"][0]["status"] == "applied", retained
    home.round()
    home.enter()


def _review_at_configured_delay(home, task, monkeypatch, verdict):
    """The dispatcher's own maintenance entry with ``kanban.spend_review_after_seconds: 300`` read from the
    isolated HERMES_HOME config: nothing starts before the routed notice is 300 s old; the review runs after."""
    from hermes_cli import kanban_dispatch_maintenance as dispatch
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli import kanban_spend_reviewer as reviewer
    config = home.home / "config.yaml"
    config.write_text(config.read_text().replace("kanban:\n", "kanban:\n  spend_review_after_seconds: 300\n", 1))
    assert reviewer.review_after_seconds() == 300
    monkeypatch.setattr(reviewer, "judge", lambda ctx: verdict)
    stop = decision.pending_episode(home.conn, task)
    assert stop is not None
    with kb.write_txn(home.conn):
        decision.safe_ensure_producer(home.conn, task)
    noticed = home.conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind=?",
                                (task, decision.NOTICE)).fetchone()[0]
    assert noticed is not None
    started = (reviewer.QUEUED, reviewer.STARTED)
    dispatch._start_spend_reviews(home.conn)  # the real clock: the notice is seconds old
    assert reviewer.start_due(home.conn, now=noticed + 299) == []
    assert not home.conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN (?,?)",
                                 (task, *started)).fetchone()
    from hermes_cli import kanban_spend_caused_review as caused
    why = {"eligible": reviewer.eligible(home.conn, task, stop["uid"]), "fenced": caused.fenced(home.conn, task),
           "grants": (caused.grant_count(home.conn, task), reviewer.consecutive_grants(home.conn, task)),
           "cause": caused.effective_cause(home.conn, task, stop), "state": reviewer.state(home.conn, task, stop["uid"]),
           "human": reviewer._human_after_stop(home.conn, task, stop), "notice": reviewer._notice_at(home.conn, task, stop["uid"]),
           "noticed": noticed, "settings": reviewer.settings(), "disposed": reviewer._disposed(home.conn, task, stop["uid"]),
           "route": decision.episode_route(home.conn, task, stop["uid"]),
           "admin": home.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='administrative_decision'", (task,)).fetchone()[0]}
    import json
    assert reviewer.start_due(home.conn, now=noticed + 300) == [task], json.dumps(why, default=str)
    for thread in list(reviewer._THREADS):
        thread.join(120)
        assert not thread.is_alive()
    home.reopen()
    return stop["uid"]


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_scope_stop_reviewed_after_configured_delay_releases_and_claims(composed, lane, monkeypatch, spend_reviewer):
    home, pool, task = composed
    first = home.hub.reviewer_allowance(base.BOARD, task)["allowance"]["review_uid"]
    _stop_again(home, task, QUOTE)
    episode = _review_at_configured_delay(home, task, monkeypatch, {
        "decision": "release", "reason": "Finish the integration proof", "remaining_quote": QUOTE})
    current = home.hub.reviewer_allowance(base.BOARD, task)["allowance"]
    assert current["review_uid"] != first and current["episode"] == episode
    assert (current["used"], current["remaining"]) == (0, 10)
    home.round()
    if lane == "review":
        result = home.hub.ops(base.BOARD, [{"client_op_id": uid(), "op": "request_review", "task": task}])
        assert result["results"][0]["status"] == "applied", result
    pool.round()
    pool.reopen()
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(pool.conn, task, ttl_seconds=900, claimer="fixture:delayed") is not None, sync.pop_claim_deferral(task)
    after = pool.hub.reviewer_allowance(base.BOARD, task)["allowance"]
    assert (after["used"], after["remaining"]) == (1, 9)


def test_duplicate_named_in_the_stop_is_archived_without_any_grant(composed, monkeypatch, spend_reviewer):
    home, pool, task = composed
    home.enter()
    replacement = kb.create_task(home.conn, title="the finished replacement", assignee="default")
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (replacement,))
    home.round()
    before = home.hub.reviewer_allowance(base.BOARD, task)["allowance"]["review_uid"]
    _stop_again(home, task, f"This card duplicates {replacement} which already finished the acceptance work.")
    _review_at_configured_delay(home, task, monkeypatch, {
        "decision": "archive", "reason": "duplicate of the finished replacement", "target": replacement})
    home.round()
    snap = home.hub._call("GET", f"/v2/boards/{base.BOARD}/snapshot")
    assert next(t for t in snap["tasks"] if t["uid"] == task)["status"] == "archived"
    left = home.hub.reviewer_allowance(base.BOARD, task)["allowance"]
    assert left is None or (left["review_uid"], left["used"]) == (before, 1)  # no new allowance, no further debit
    pool.round()
    pool.reopen()
    assert kb.claim_task(pool.conn, task, ttl_seconds=900, claimer="fixture:archived") is None
