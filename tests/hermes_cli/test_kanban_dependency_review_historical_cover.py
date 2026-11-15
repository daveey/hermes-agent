"""A captured PR approved before the park by a now-historical parent, still at that head, resumes once.

Production shape (2026-11-13, t_01a1d4635554fa12): the owner published core PR #2097 and
state PR #2108 (cited only by number) and filed review R1 for #2097 and R2 for #2108. R1
approved #2097 at its exact head BEFORE the owner parked; the park awaited only R2 and
captured only #2097. R2 then approved #2108 at its exact head. The owner stayed
``active_pr``-held for over an hour: R1 was historical and R2 named an uncaptured PR.
"""
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_dependency_review_historical as hist
from hermes_cli import kanban_host_identity as host_identity
from hermes_cli.kanban_dependency_review import resumes_review
from tests.hermes_cli import test_kanban_dependency_review as helpers
from tests.hermes_cli.kanban_guard_helpers import worker_comment

board = helpers.board
CORE = "https://github.com/softmax-universe/daveey-hermes/pull/2097"
STATE = "https://github.com/softmax-universe/daveey-hermes/pull/2108"
HEAD = "e826c065ce30b3f82370123a382e40c413d16b50"
STATE_HEAD = "3a4f5f1de7" + "0" * 30
MOVED = "b" * 40
OTHER = "https://github.com/softmax-universe/daveey-hermes/pull/2099"
OTHER_HEAD = "c" * 40
_REAL_GH_PR_STATE = prs._gh_pr_state


@pytest.fixture(params=["native", "darwin"])
def host_platform(request, monkeypatch):
    """Exercise cold macOS identity lookup without changing pytest's platform."""
    real_boot_prefix = host_identity.boot_prefix
    real_boot_prefix.cache_clear()
    if request.param == "darwin":
        monkeypatch.setattr(host_identity, "sys", SimpleNamespace(platform="darwin"))
    yield
    real_boot_prefix.cache_clear()


@pytest.fixture(autouse=True)
def gh(monkeypatch, host_platform):
    """Fake ``gh pr view``: ``gh.heads[url]`` is the PR's head; ``gh.fail`` fails every read."""
    class Gh:
        heads = {CORE: HEAD, STATE: STATE_HEAD}
        fail = False
        calls: list = []

    Gh.calls = []

    def run(argv, **kwargs):
        Gh.calls.append(list(argv))
        if Gh.fail:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 502")
        url = next(a for a in argv if a.startswith("https://"))
        out = {"state": "OPEN", "headRefOid": Gh.heads.get(url), "comments": []}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")

    # Claim ownership is unrelated to PR history. Do not send macOS sysctl
    # through the gh-only subprocess fake (or rely on a warmed boot cache).
    monkeypatch.setattr(host_identity, "boot_prefix", lambda: None)
    monkeypatch.setattr(prs, "_gh_pr_state", _REAL_GH_PR_STATE)
    monkeypatch.setattr(prs, "_gh_argv", lambda: ["gh"])
    monkeypatch.setattr(subprocess, "run", run)
    prs.clear_cache()
    yield Gh
    prs.clear_cache()


def finish(conn, review, metadata, *, claim=True):
    run = None
    if claim:
        kb.claim_task(conn, review)
        run = kb.get_task(conn, review).current_run_id
    assert kb.complete_task(conn, review, summary="Review complete", expected_run_id=run, metadata=metadata)
    kb.recompute_ready(conn)


def approve(pr, sha):
    return {"verdict": "approve", "reviewed_sha": sha, "reviewed_pr": pr}


def incident(conn, *, r1=None, r2=None, prerequisite=True, r1_claim=True, r2_claim=True, before_park=None,
             extra_pr=None):
    """Replay the sequence: R1 approves CORE pre-park, owner parks awaiting R2, R2 finishes."""
    now = int(time.time())
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(time, "time", lambda: now - 30)
        task = kb.create_task(conn, title="worker disk reclaim", assignee="dev")
        parents = []
        if prerequisite:
            parents.append(kb.create_task(conn, title="design ruling", assignee="dev"))
            finish(conn, parents[0], {} if prerequisite is True else prerequisite)
        kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        worker_comment(conn, task, "dev", "Published core PR " + CORE)
        if extra_pr:
            worker_comment(conn, task, "dev", "Follow-up PR " + extra_pr)
        worker_comment(conn, task, "dev", "State PR #2108 filed too")
        review_1 = kb.create_task(conn, title="review #2097", assignee="reviewer")
        review_2 = kb.create_task(conn, title="review #2108", assignee="reviewer")
        for parent in [*parents, review_1, review_2]:
            kb.link_tasks(conn, parent, task, expected_child_run_id=run)
        clock.setattr(time, "time", lambda: now - 20)
        finish(conn, review_1, approve(CORE, HEAD) if r1 is None else r1, claim=r1_claim)
        if before_park is not None:
            before_park(conn, review_1)
        clock.setattr(time, "time", lambda: now - 10)
        assert kb.block_task(conn, task, kind="dependency", reason="review #2108", expected_run_id=run)
    checkpoint = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (task,),
    ).fetchone()["payload"])["review_checkpoint"]
    assert checkpoint["awaited"] == [review_2] and checkpoint["prs"] == sorted({CORE, extra_pr or CORE})
    finish(conn, review_2, approve(STATE, STATE_HEAD) if r2 is None else r2, claim=r2_claim)
    assert kb.get_task(conn, task).status == "ready"
    return task


def test_incident_resumes_exactly_once(board, gh):
    task = incident(board)
    assert resumes_review(board, task, [CORE]) is True
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_incident_without_prerequisite_parent_resumes(board):
    task = incident(board, prerequisite=False)
    assert dispatch.check_respawn_guard(board, task) is None


def test_head_moved_after_historical_approval_stays_held(board, gh):
    gh.heads = {CORE: MOVED, STATE: STATE_HEAD}
    task = incident(board)
    assert resumes_review(board, task, [CORE]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_head_moved_after_a_cached_match_stays_held(board, gh, monkeypatch):
    task = incident(board)
    assert prs.pr_head(CORE) == HEAD  # a match cached by an earlier tick, a minute ago
    prs._heads.update({k: (v[0], v[1] - 60) for k, v in prs._heads.items()})
    gh.heads = {CORE: MOVED, STATE: STATE_HEAD}
    assert resumes_review(board, task, [CORE]) is False


def test_gh_failure_stays_held(board, gh):
    gh.fail = True
    task = incident(board)
    assert resumes_review(board, task, [CORE]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("r2", [
    {"verdict": "approve", "reviewed_pr": STATE},  # no head at all
    {"verdict": "approve", "reviewed_pr": STATE, "reviewed_sha": "3a4f5f1de7"},  # short head
    {"reviewed_pr": STATE, "reviewed_sha": STATE_HEAD},  # no verdict: not explicit evidence
    {"verdict": "looks fine", "reviewed_pr": STATE, "reviewed_sha": STATE_HEAD},
])
def test_awaited_parent_without_explicit_exact_head_verdict_stays_held(board, r2):
    task = incident(board, r2=r2)
    assert resumes_review(board, task, [CORE]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_awaited_change_request_on_sibling_is_ordering_proof(board):
    r2 = {"verdict": "request_changes", "reviewed_pr": STATE, "reviewed_sha": STATE_HEAD}
    task = incident(board, r2=r2)
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("r1", [
    {"verdict": "request_changes", "reviewed_pr": CORE, "reviewed_sha": HEAD},
    {"verdict": "approve", "reviewed_pr": STATE, "reviewed_sha": HEAD},  # names another PR
    {"verdict": "approve", "reviewed_sha": HEAD},  # names no PR: the checkpoint post-dates it
    {"verdict": "approve", "reviewed_pr": CORE, "head": HEAD},  # no reviewed_sha to compare
    {"verdict": "approve", "reviewed_pr": CORE, "reviewed_sha": HEAD[:10]},
    {"verdict": "approve", "reviewed_pr": CORE, "reviewed_sha": HEAD, "approved": False},
])
def test_historical_parent_must_approve_that_pr_at_a_full_head(board, r1):
    task = incident(board, r1=r1)
    assert resumes_review(board, task, [CORE]) is False


def test_historical_parent_reopened_after_park_does_not_cover(board):
    task = incident(board)
    review_1 = _review_1(board, task)
    kb._append_event(board, review_1, "reopened", {})
    assert resumes_review(board, task, [CORE]) is False


def test_uncovered_second_captured_pr_stays_held(board, gh):
    # The awaited parent's sibling approval never covers a captured PR history doesn't cover.
    task = incident(board, extra_pr=OTHER)
    prs.clear_cache()
    gh.calls.clear()
    assert resumes_review(board, task, [CORE, OTHER]) is False
    assert gh.calls == []  # refused before any GitHub read
    assert resumes_review(board, task, [CORE]) is True  # the covered PR alone would resume


@pytest.mark.parametrize("r1", [
    {"reviewed_pr": CORE, "reviewed_sha": HEAD},  # no verdict
    {"verdict": "approve", "reviewed_pr": CORE},  # no reviewed_sha
])
def test_historical_parent_needs_verdict_and_head(board, r1):
    task = incident(board, r1=r1)
    assert resumes_review(board, task, [CORE]) is False


def test_unclaimed_historical_completion_does_not_cover(board):
    task = incident(board, r1_claim=False)
    assert resumes_review(board, task, [CORE]) is False


def test_historical_run_that_did_not_complete_does_not_cover(board):
    def timed_out(conn, review):
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET outcome = 'timed_out' WHERE task_id = ?", (review,))
    task = incident(board, before_park=timed_out)
    assert resumes_review(board, task, [CORE]) is False


def test_unclaimed_awaited_completion_is_not_ordering_proof(board):
    task = incident(board, r2_claim=False)
    assert resumes_review(board, task, [CORE]) is False


def test_legacy_checkpoint_without_awaited_list_stays_held(board):
    task = incident(board)
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? "
                        "AND kind = 'dependency_wait'", (task,)).fetchone()
    payload = json.loads(row["payload"])
    payload["review_checkpoint"].pop("awaited")
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    assert resumes_review(board, task, [CORE]) is False


def test_sibling_approval_of_unrelated_pr_still_approves_nothing(board):
    # An awaited parent approves the holding PR; its awaited sibling approved a PR the
    # checkpoint never captured. History has nothing to cover, so the caller's rule ("a named
    # unrelated PR approves nothing") stands.
    from tests.hermes_cli import test_kanban_dependency_review_mixed as mixed
    from tests.hermes_cli import test_kanban_dependency_review_split as split
    task, reviews = split.parked_two_prs(board)
    mixed.finish(board, reviews, [mixed.approve(split.CORE), mixed.approve(split.MODS + "1")])
    assert resumes_review(board, task, [split.CORE]) is False


def test_unparked_crash_after_review_is_not_a_handoff(board, gh):
    task = incident(board)
    assert kb.claim_task(board, task) is not None
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert resumes_review(board, task, [CORE]) is False


def test_each_approval_covers_only_the_pr_it_names(board, gh):
    # The prerequisite approved OTHER at OTHER_HEAD before the park; CORE then moved to a head
    # equal to that sha. Its approval of OTHER never covers CORE.
    gh.heads = {CORE: OTHER_HEAD, OTHER: OTHER_HEAD, STATE: STATE_HEAD}
    task = incident(board, prerequisite=approve(OTHER, OTHER_HEAD), extra_pr=OTHER)
    assert resumes_review(board, task, [CORE]) is False
    assert resumes_review(board, task, [OTHER]) is True


def _park(board, task):
    """(parked_at, owner run id, review_checkpoint) of the owner's dependency park."""
    row = board.execute("SELECT run_id, created_at, payload FROM task_events WHERE task_id = ? "
                        "AND kind = 'dependency_wait'", (task,)).fetchone()
    return kb._to_epoch(row["created_at"]), row["run_id"], json.loads(row["payload"])["review_checkpoint"]


def _review_1(board, task):
    return next(p for p in kb.parent_ids(board, task) if kb.get_task(board, p).title == "review #2097")


def test_historical_approval_requires_unchanged_history(board):
    task = incident(board)
    parked_at, _, _ = _park(board, task)
    review_1 = _review_1(board, task)
    assert hist._historical_approval(board, review_1, parked_at, {CORE}) == ({CORE}, HEAD)
    kb._append_event(board, review_1, "reopened", {})
    assert hist._historical_approval(board, review_1, parked_at, {CORE}) is None


def test_historical_approval_requires_a_full_sha(board):
    task = incident(board, r1={"verdict": "approve", "reviewed_pr": CORE, "reviewed_sha": HEAD[:10]})
    parked_at, _, _ = _park(board, task)
    assert hist._historical_approval(board, _review_1(board, task), parked_at, {CORE}) is None


def test_covers_needs_an_awaited_parent(board, gh):
    task = incident(board)
    parked_at, run_id, checkpoint = _park(board, task)
    kwargs = dict(checkpointed=checkpoint["parents"], named_awaited=checkpoint["awaited"], urls={CORE},
                  covered=set(), parked_at=parked_at, owner_run_id=run_id, captured={CORE})
    assert hist.covers(board, parents=checkpoint["awaited"], **kwargs) is True
    assert hist.covers(board, parents=[], **kwargs) is False
