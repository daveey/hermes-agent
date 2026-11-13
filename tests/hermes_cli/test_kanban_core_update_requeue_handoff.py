"""A hermes-sync core-update requeue must not consume a PR owner's one-shot handoff. [daveey]

Production (2026-11-12, worker-5): the owner of PR #1271 parked on a re-review, the review
approved, the card was promoted and claimed, and 41 s later the core updater's restart-safe
requeue reclaimed that run. The dispatcher then held the card ``active_pr`` with no worker
until an operator intervened. That requeue is a deliberate pause, not a crash: the next
respawn resumes as if the requeued run never happened. A real crash keeps the hold.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.hermes_cli import test_kanban_dependency_review as dr

board = dr.board
HEAD, PR, finish_reviews, parked = dr.HEAD, dr.PR, dr.finish_reviews, dr.parked

CRASH = "Worker died"


def _approved(board):
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"verdict": "APPROVE", "head": HEAD, "pr": PR})
    kb.add_comment(board, task, "reviewer", "APPROVE at exact head " + HEAD + " " + PR)
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    return task


def _ordering(board):
    """An ordering (non-review) dependency: the parent's result is no review verdict."""
    task, reviews = parked(board)
    finish_reviews(board, reviews, metadata={"changed_files": ["a.py"]})
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    return task


def _reclaim(board, task, reason):
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=reason, signal_fn=lambda *_: None)
    assert kb.get_task(board, task).status == "ready"


@pytest.mark.parametrize("setup", [_approved, _ordering], ids=["review_checkpoint", "ordering"])
def test_core_update_requeue_keeps_the_handoff(board, setup):
    task = setup(board)
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    assert dispatch.check_respawn_guard(board, task) is None
    # Twice in a row (two drains) still resumes; the next real claim consumes it.
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("setup", [_approved, _ordering], ids=["review_checkpoint", "ordering"])
@pytest.mark.parametrize("reason", [CRASH, None, CORE_UPDATE_REQUEUE_REASON + " (by hand)"])
def test_crash_or_other_reclaim_still_consumes_the_handoff(board, setup, reason):
    task = setup(board)
    _reclaim(board, task, reason)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("setup", [_approved, _ordering], ids=["review_checkpoint", "ordering"])
def test_requeued_run_that_published_a_new_pr_gets_one_continuation(board, setup):
    """A PR the requeued run reported is its own: the requeue (a pause) gets one continuation.

    Production 2026-11-13 (t_01a1d1d7998cda2a): run 20713 opened PR #1783, reported it and was
    requeued 9 min later; the card then sat held ``active_pr`` with no worker. The next real
    claim consumes the continuation, so a crash of it holds again.
    """
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = setup(board)
    assert kb.claim_task(board, task) is not None
    worker_comment(board, task, "dev", "Opened " + PR + "7")
    assert kb.reclaim_task(board, task, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def _publisher(board, *, prior_continue=False, writer="dev", reason=CORE_UPDATE_REQUEUE_REASON):
    """A card whose run publishes its PR and is then reclaimed with ``reason``.

    ``prior_continue``: an earlier run ended ``continued`` before the PR (the incident's run 20711)."""
    import time

    from hermes_cli import kanban_continuation as continuation
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = kb.create_task(board, title="implementation", assignee="dev")
    if prior_continue:
        published = int(time.time())
        with pytest.MonkeyPatch.context() as clock:
            clock.setattr(time, "time", lambda: published - 5)
            kb.claim_task(board, task)
            run = kb.get_task(board, task).current_run_id
            assert continuation.continue_task(board, task, checkpoint="next: open the PR", expected_run_id=run,
                                              resume_after_seconds=0)["ok"]
    assert kb.claim_task(board, task) is not None
    worker_comment(board, task, writer, "Opened " + PR)
    assert kb.reclaim_task(board, task, reason=reason, signal_fn=lambda *_: None)
    assert kb.get_task(board, task).status == "ready"
    return task


def _latest_run(board, task):
    return board.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task,)).fetchone()


@pytest.mark.parametrize("prior_continue", [False, True], ids=["first_run", "after_continued"])
def test_requeued_publisher_resumes_once_and_its_crash_holds(board, prior_continue):
    task = _publisher(board, prior_continue=prior_continue)
    assert dispatch.check_respawn_guard(board, task) is None
    # Another drain before the continuation runs anything is still a pause.
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    assert dispatch.check_respawn_guard(board, task) is None
    # The next real claim consumes the grant: its crash holds again.
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_requeued_publisher_continuation_that_timed_out_holds(board):
    task = _publisher(board)
    _reclaim(board, task, CRASH)
    run = _latest_run(board, task)["id"]
    with kb.write_txn(board):
        kb._append_event(board, task, "timed_out", {"elapsed": 1}, run_id=run)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("reason", [CRASH, None, CORE_UPDATE_REQUEUE_REASON + " (by hand)"])
def test_publisher_crash_or_other_reclaim_holds(board, reason):
    task = _publisher(board, reason=reason)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("kind", ["crashed", "timed_out"])
def test_publisher_with_failure_evidence_holds(board, kind):
    task = _publisher(board)
    with kb.write_txn(board):
        kb._append_event(board, task, kind, {"pid": 1}, run_id=_latest_run(board, task)["id"])
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_publisher_of_another_profile_holds(board):
    task = _publisher(board)
    board.execute("UPDATE tasks SET assignee = 'other' WHERE id = ?", (task,))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def _restamp(board, task, **fields):
    """Rewrite the writer provenance on the card's newest ``commented`` event."""
    import json

    event_id, payload = board.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'commented' ORDER BY id DESC LIMIT 1",
        (task,)).fetchone()
    data = json.loads(payload)
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(data), event_id))


def test_publisher_with_unknown_writer_uid_holds(board):
    """A writer uid naming no local run (another gateway's card, or lost) is unknown: held."""
    task = _publisher(board)
    _restamp(board, task, writer_run_uid="01a1d221-0000-7000-8000-000000000000")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_publisher_with_unmatched_comment_event_holds(board):
    """A PR comment whose ``commented`` event maps to no row has no writer: held."""
    task = _publisher(board)
    _restamp(board, task, author="not-the-row-author")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_publisher_with_duplicate_comment_events_holds(board):
    task = _publisher(board)
    payload, run = board.execute(
        "SELECT payload, run_id FROM task_events WHERE task_id = ? AND kind = 'commented'", (task,)).fetchone()
    import json
    with kb.write_txn(board):
        kb._append_event(board, task, "commented", json.loads(payload), run_id=run)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_pr_from_an_earlier_crashed_run_holds_after_a_requeue(board):
    """The requeued run did not write the holding comment: an earlier crashed run did."""
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = kb.create_task(board, title="implementation", assignee="dev")
    assert kb.claim_task(board, task) is not None
    worker_comment(board, task, "dev", "Opened " + PR)
    assert kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)  # an operator-claimed run, then requeued
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_second_publisher_holds(board):
    """A newer holding PR comment from another (unknown) writer than the requeued run: held."""
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = _publisher(board)
    worker_comment(board, task, "dev", "Also " + PR + "8")
    _restamp(board, task, writer_run_uid="01a1d221-0000-7000-8000-000000000001")
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_chat_pr_note_after_the_requeue_stays_fyi(board):
    """A chat comment is FYI to the guard (unchanged): the requeued publisher still continues."""
    task = _publisher(board)
    kb.add_comment(board, task, "operator", "FYI " + PR + "9")
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("writer", ["chat", "other_card_run"])
def test_chat_or_other_card_pr_note_cannot_grant_after_a_crash(board, writer):
    """Only the requeued run's own PR comment grants: a chat or another card's run (FYI) never does."""
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = kb.create_task(board, title="implementation", assignee="dev")
    assert kb.claim_task(board, task) is not None
    worker_comment(board, task, "dev", "Opened " + PR)
    assert kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    if writer == "chat":
        kb.add_comment(board, task, "dev", "FYI " + PR)
    else:
        other = kb.create_task(board, title="reviewer", assignee="dev")
        assert kb.claim_task(board, other) is not None
        foreign_run = kb.get_task(board, other).current_run_id
        with pytest.MonkeyPatch.context() as env:
            env.setenv("HERMES_KANBAN_TASK", task)  # stamps the other card's run as writer
            env.setenv("HERMES_KANBAN_RUN_ID", str(foreign_run))
            kb.add_comment(board, task, "dev", "Reviewing " + PR)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


_UID = "01a1d221-8df1-7f34-947f-"


def _synced_publisher(board, mutate=None):
    """The incident on a Board-synced schema: runs carry a portable uid, comments name it."""
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    board.execute("ALTER TABLE task_runs ADD COLUMN uid TEXT")
    task = kb.create_task(board, title="implementation", assignee="dev")
    assert kb.claim_task(board, task) is not None
    board.execute("UPDATE task_runs SET uid = ? || printf('%012d', id) WHERE task_id = ?", (_UID, task))
    worker_comment(board, task, "dev", "Opened " + PR)
    if mutate:
        mutate(board, task)
    assert kb.reclaim_task(board, task, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=lambda *_: None)
    return task


def test_synced_requeued_publisher_resumes_once(board):
    task = _synced_publisher(board)
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def _other_card_uid(board, task):
    other = kb.create_task(board, title="reviewer", assignee="dev")
    assert kb.claim_task(board, other) is not None
    board.execute("UPDATE task_runs SET uid = ? WHERE task_id = ?", (_UID + "999999999999", other))
    _restamp(board, task, writer_run_uid=_UID + "999999999999")
    # Even a core-update-requeued run of the assignee's profile grants nothing on another card.
    assert kb.reclaim_task(board, other, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=lambda *_: None)


def _non_text_uid(board, task):  # a number never names a run, even one whose text uid reads the same
    board.execute("UPDATE task_runs SET uid = '7' WHERE task_id = ?", (task,))
    _restamp(board, task, writer_run_uid=7)


def _uid_less_run(board, task):
    board.execute("UPDATE task_runs SET uid = NULL WHERE task_id = ?", (task,))
    _restamp(board, task, writer_run_uid=None)


@pytest.mark.parametrize("mutate", [
    lambda board, task: _restamp(board, task, writer_run_uid=None),  # an integer cannot vouch here
    lambda board, task: _restamp(board, task, writer_run_uid=_UID + "000000000777"),  # no such run
    _non_text_uid,
    _uid_less_run,
], ids=["integer_only", "unknown_uid", "non_text_uid", "uid_less_run"])
def test_synced_publisher_with_unproven_writer_holds(board, mutate):
    task = _synced_publisher(board, mutate)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_synced_other_card_writer_is_fyi_and_grants_nothing(board):
    """Another card's run is FYI (unchanged): with no own PR comment there is nothing to hold."""
    task = _synced_publisher(board, _other_card_uid)
    assert dispatch.check_respawn_guard(board, task) is None
    from hermes_cli import kanban_requeued_publisher as publisher
    row = board.execute("SELECT id FROM task_comments WHERE task_id = ?", (task,)).fetchone()[0]
    assert not publisher.continues(board, task, [(0, row)], row)


def test_publisher_with_an_unreadable_comment_event_holds(board):
    from hermes_cli import kanban_requeued_publisher as publisher

    task = _publisher(board)
    row = board.execute("SELECT id FROM task_comments WHERE task_id = ?", (task,)).fetchone()[0]
    assert publisher.continues(board, task, [(0, row)], row)
    with kb.write_txn(board):
        kb._append_event(board, task, "commented", {"comment_id": "x"})
    assert not publisher.continues(board, task, [(0, row)], row)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_publisher_with_unreadable_start_holds(board):
    from hermes_cli import kanban_requeued_publisher as publisher

    task = _publisher(board)
    row = board.execute("SELECT id FROM task_comments WHERE task_id = ?", (task,)).fetchone()[0]
    assert publisher.continues(board, task, [(0, row)], row)
    board.execute("UPDATE task_runs SET started_at = 'later' WHERE task_id = ?", (task,))
    assert not publisher.continues(board, task, [(0, row)], row)


def test_pr_named_by_an_earlier_handed_off_run_then_restated_by_the_publisher(board):
    """Comments from before the requeued run's claim were cleared at that claim: only its own count."""
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = kb.create_task(board, title="implementation", assignee="dev")
    assert kb.claim_task(board, task) is not None
    worker_comment(board, task, "dev", "Opened " + PR)
    board.execute("UPDATE task_comments SET created_at = created_at - 30 WHERE task_id = ?", (task,))
    board.execute("UPDATE task_events SET created_at = created_at - 30 WHERE task_id = ? AND kind = 'commented'",
                  (task,))
    kb.reclaim_task(board, task, reason=CRASH, signal_fn=lambda *_: None)
    with kb.write_txn(board):  # an operator unblock after the crash: the next claim may resume the PR
        kb._append_event(board, task, "unblocked", {"by": "operator"})
    board.execute("UPDATE task_events SET created_at = created_at - 20 WHERE task_id = ? AND kind = 'unblocked'",
                  (task,))
    board.execute("UPDATE task_runs SET started_at = started_at - 40, ended_at = ended_at - 25 WHERE task_id = ?",
                  (task,))
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    worker_comment(board, task, "dev", "Still on " + PR)
    assert kb.reclaim_task(board, task, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) is None


def test_another_own_run_writing_since_the_publisher_claim_holds(board):
    """A second writer that is a known run of this card (not the requeued one): held."""
    import json

    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    task = _publisher(board, prior_continue=True)
    requeued = _latest_run(board, task)["id"]
    first = board.execute("SELECT MIN(id) FROM task_runs WHERE task_id = ?", (task,)).fetchone()[0]
    event_id, payload = board.execute(  # the requeued run's PR comment, restamped to the earlier run
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'commented'", (task,)).fetchone()
    board.execute("UPDATE task_events SET payload = ? WHERE id = ?",
                  (json.dumps({**json.loads(payload), "writer_run_id": first}), event_id))
    worker_comment(board, task, "dev", "Also " + PR + "8")  # the newest is the requeued run's own
    assert _latest_run(board, task)["id"] == requeued
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_dispatcher_respawns_requeued_publisher(board, monkeypatch):
    spawned: list[str] = []
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: (lambda _name: True))
    task = _publisher(board, prior_continue=True)
    result = dispatch.dispatch_once(board, spawn_fn=lambda t, _ws, board=None: spawned.append(t.id))
    assert spawned == [task] and task not in [tid for tid, _ in result.respawn_guarded]


def test_crash_event_on_requeued_run_is_not_neutral(board):
    task = _ordering(board)
    _reclaim(board, task, CORE_UPDATE_REQUEUE_REASON)
    run = board.execute("SELECT MAX(id) FROM task_runs WHERE task_id = ?", (task,)).fetchone()[0]
    with kb.write_txn(board):
        kb._append_event(board, task, "crashed", {"pid": 1}, run_id=run)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("reason,held", [(CORE_UPDATE_REQUEUE_REASON, None), (CRASH, "active_pr")],
                         ids=["core_update_requeue", "crash"])
def test_same_second_immediate_checkpoint_survives_requeue(board, monkeypatch, reason, held):
    """The same-second ``kanban_continue`` handoff (latest ended run) skips a requeue too."""
    import time

    from hermes_cli import kanban_continuation as continuation
    from tests.hermes_cli.kanban_guard_helpers import worker_comment

    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    task = kb.create_task(board, title="implementation", assignee="dev")
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    worker_comment(board, task, "dev", "Published " + PR)
    result = continuation.continue_task(board, task, checkpoint="PR open, next: CI", expected_run_id=run,
                                        resume_after_seconds=0)
    assert result["ok"], result
    assert dispatch.check_respawn_guard(board, task) is None
    _reclaim(board, task, reason)
    assert dispatch.check_respawn_guard(board, task) == held


@pytest.mark.parametrize("reason,respawns", [(CORE_UPDATE_REQUEUE_REASON, True), (CRASH, False)],
                         ids=["core_update_requeue", "crash"])
def test_dispatcher_respawns_after_core_update_requeue(board, monkeypatch, reason, respawns):
    """Incident sequence through the real dispatcher tick: park -> APPROVE -> claim -> reclaim."""
    spawned: list[str] = []
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: (lambda _name: True))
    task = _approved(board)
    _reclaim(board, task, reason)
    result = dispatch.dispatch_once(board, spawn_fn=lambda t, _ws, board=None: spawned.append(t.id))
    guarded = [tid for tid, _ in result.respawn_guarded]
    if respawns:
        assert spawned == [task] and task not in guarded
        assert not [e for e in kb.list_events(board, task) if e.kind == "respawn_guarded"]
    else:
        assert spawned == [] and (task, "active_pr") in result.respawn_guarded
