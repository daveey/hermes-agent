"""PR owners whose handoff the active_pr guard missed (2026-11-12/13 live holds). [daveey]

* Same second: the owner reports its PR and parks (``kanban_schedule``/``kanban_continue``)
  within one second (t_01a1d30035fa4777: comment 15615 and its ``scheduled`` event both at
  1794542252). #1595's tests always advanced the clock between comment and park.
* Awaited review: the park waits on a review card it is not linked to; that review approved
  the PR at its current head (t_01a1d3093ee49e78 for PR #1862).
* Historical review parent: a dependency park whose approval parent finished before the
  park held after the awaited rollout parent finished (t_01a1d09d3547337b, state PR #1760).
"""
import json
import time

import pytest

from hermes_cli import kanban_awaited_review as kar
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")
URL = "https://github.com/example/project/pull/12"
OTHER = "https://github.com/example/project/pull/13"
HEAD = "a" * 40
_REAL_GH_PR_STATE = prs._gh_pr_state


@pytest.fixture
def clock(monkeypatch):
    now = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    monkeypatch.setattr(kar, "pr_head", lambda url, **_: HEAD)
    return now


def _comment(conn, tid, run_id, body, monkeypatch, *, task=None):
    with monkeypatch.context() as env:
        env.setenv("HERMES_KANBAN_TASK", task or tid)
        env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return kb.add_comment(conn, tid, "a", body)


def _park(conn, tid, kind, wait_on=None):
    if kind == "scheduled":
        kwargs = {"wake_check": f"hermes kanban wait-check task {wait_on} --gateway worker-3",
                  "check_every": 900} if wait_on else {"clock_only": True,
                                                       "awaiting": {"kind": "clock", "why": "review window"}}
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on review",
                                **kwargs)
    else:
        assert kc.continue_task(conn, tid, checkpoint="wait for review", resume_after_seconds=60)["ok"]


def _owner(conn, monkeypatch, *, synced=False):
    if synced:
        conn.execute("ALTER TABLE task_runs ADD COLUMN uid TEXT")
    tid = kb.create_task(conn, title="finish owned PR", assignee="a")
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    if synced:
        conn.execute("UPDATE task_runs SET uid = '01a1d309-ddac-7273-96fe-' || printf('%012d', id) WHERE id=?",
                     (run_id,))
    return tid, run_id


def _wake(conn, tid):
    """The timed park's wake: the card is READY with no worker."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                     "claim_expires=NULL WHERE id=?", (tid,))


# --- same-second report + park --------------------------------------------------------

@pytest.mark.parametrize("synced", [False, True], ids=["local", "synced"])
@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_same_second_report_then_park_by_the_same_run_resumes_once(clock, monkeypatch, kind, synced):
    with kbc.connect() as conn:
        tid, run_id = _owner(conn, monkeypatch, synced=synced)
        _comment(conn, tid, run_id, f"Opened {URL}; waiting on review", monkeypatch)
        _park(conn, tid, kind)
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) is None
        # One continuation: the next claim consumes it, a crash of that run does not renew it.
        kb.claim_task(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"
        assert kb.reclaim_task(conn, tid, reason="worker died", signal_fn=lambda *_: None)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_same_second_pr_comment_after_the_park_still_guards(clock, monkeypatch, kind):
    with kbc.connect() as conn:
        tid, run_id = _owner(conn, monkeypatch)
        _comment(conn, tid, run_id, f"Opened {URL}", monkeypatch)
        _park(conn, tid, kind)
        _comment(conn, tid, run_id, f"Also opened {OTHER}", monkeypatch)
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


def test_same_second_pr_comment_from_an_earlier_run_still_guards(clock, monkeypatch):
    """A lingering earlier run of the card wrote the PR comment; another run parked."""
    with kbc.connect() as conn:
        tid, first = _owner(conn, monkeypatch)
        assert kc.continue_task(conn, tid, checkpoint="next run", resume_after_seconds=0)["ok"]
        clock[0] += 5
        kb.claim_task(conn, tid)
        _comment(conn, tid, first, f"Opened {URL}", monkeypatch)
        _park(conn, tid, "scheduled")
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


@pytest.mark.parametrize("mutation", ["no_event", "two_events", "foreign_uid", "park_other_run"])
def test_same_second_tie_with_unprovable_order_still_guards(clock, monkeypatch, mutation):
    with kbc.connect() as conn:
        tid, run_id = _owner(conn, monkeypatch, synced=mutation == "foreign_uid")
        cid = _comment(conn, tid, run_id, f"Opened {URL}", monkeypatch)
        _park(conn, tid, "scheduled")
        _wake(conn, tid)
        with kb.write_txn(conn):
            event = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='commented'",
                                 (tid,)).fetchone()
            if mutation == "no_event":
                conn.execute("DELETE FROM task_events WHERE id=?", (event["id"],))
            elif mutation == "two_events":
                conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                             "SELECT task_id, run_id, kind, payload, created_at FROM task_events WHERE id=?",
                             (event["id"],))
            elif mutation == "foreign_uid":
                payload = json.loads(event["payload"])
                payload["writer_run_uid"] = "01a1d309-0000-0000-0000-000000000000"
                conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
            else:
                conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='scheduled'", (tid,))
        assert cid
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


def test_unreadable_commented_event_keeps_the_tie_guarded(clock, monkeypatch):
    """An unreadable commented event could be a second event of the tie comment."""
    with kbc.connect() as conn:
        tid, run_id = _owner(conn, monkeypatch)
        _comment(conn, tid, run_id, f"Opened {URL}", monkeypatch)
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                         "VALUES (?, NULL, 'commented', 'not json', ?)", (tid, clock[0]))
        _park(conn, tid, "scheduled")
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


def test_older_pr_comment_of_an_earlier_run_does_not_block_the_same_second_handoff(clock, monkeypatch):
    """Only the comments at the newest PR second need the parking run's provenance."""
    with kbc.connect() as conn:
        tid, first = _owner(conn, monkeypatch)
        _comment(conn, tid, first, f"Opened {URL}", monkeypatch)
        assert kc.continue_task(conn, tid, checkpoint="next run", resume_after_seconds=0)["ok"]
        clock[0] += 5
        kb.claim_task(conn, tid)
        second = kb.get_task(conn, tid).current_run_id
        _comment(conn, tid, second, f"Opened follow-up {OTHER}; waiting on review", monkeypatch)
        _park(conn, tid, "scheduled")
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) is None


def test_park_in_an_earlier_second_than_the_new_pr_comment_still_guards(clock, monkeypatch):
    """Imported history can order ids against time: the park at T-5 and a NEW PR comment
    at T by the same run, its event inserted first. Only a park at the comment's second ties."""
    with kbc.connect() as conn:
        tid, run_id = _owner(conn, monkeypatch)
        _comment(conn, tid, run_id, f"Opened {URL}", monkeypatch)
        clock[0] += 5
        _comment(conn, tid, run_id, f"Also opened {OTHER}", monkeypatch)
        clock[0] -= 3
        _park(conn, tid, "scheduled")
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


def test_same_second_report_then_crash_without_park_still_guards(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, run_id = _owner(conn, monkeypatch)
        _comment(conn, tid, run_id, f"Opened {URL}", monkeypatch)
        assert kb.reclaim_task(conn, tid, reason="worker died", signal_fn=lambda *_: None)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


def test_same_second_park_of_a_non_assignee_run_still_guards(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, run_id = _owner(conn, monkeypatch)
        _comment(conn, tid, run_id, f"Opened {URL}", monkeypatch)
        _park(conn, tid, "scheduled")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='b' WHERE id=?", (tid,))
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 700) == "active_pr"


# --- awaited (unlinked) review ----------------------------------------------------------

def _finish(conn, rev, metadata):
    kb.claim_task(conn, rev)
    assert kb.complete_task(conn, rev, summary="Review complete",
                            expected_run_id=kb.get_task(conn, rev).current_run_id, metadata=metadata)


def _awaiting_owner(conn, clock, monkeypatch, metadata, *, via="wake_check", finish=True):
    """Owner reports PR and parks in the same second waiting on an unlinked review, with
    unprovable comment order (its commented event lost), so only the review can release it."""
    rev = kb.create_task(conn, title="exact-head review", assignee="reviewer")
    tid, run_id = _owner(conn, monkeypatch)
    _comment(conn, tid, run_id, f"Opened {URL}; review card {rev}", monkeypatch)
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,))
    if via == "wake_check":
        _park(conn, tid, "scheduled", wait_on=rev)
    else:
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on review",
                                clock_only=True, awaiting={"kind": "clock", "why": "review window"})
        with kb.write_txn(conn):
            row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                               (tid,)).fetchone()
            payload = json.loads(row["payload"])
            payload["awaiting"] = {"kind": "card", "ref": rev, "gateway": "worker-3"}
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    clock[0] += 60
    if finish:
        _finish(conn, rev, metadata)
    _wake(conn, tid)
    return tid, rev


APPROVE = {"verdict": "approve", "reviewed_sha": HEAD, "reviewed_pr": URL}


@pytest.mark.parametrize("via", ["wake_check", "awaiting"])
def test_awaited_review_approved_at_head_resumes_once(clock, monkeypatch, via):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE, via=via)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) is None
        kb.claim_task(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_fixture_holds_without_the_review(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE, finish=False)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


@pytest.mark.parametrize("metadata", [
    {"verdict": "request_changes", "reviewed_sha": "b" * 40, "reviewed_pr": URL},
    {"verdict": "approve", "reviewed_sha": "b" * 40, "reviewed_pr": URL},
    {"verdict": "approve", "reviewed_sha": HEAD, "reviewed_pr": OTHER},
    {"verdict": "approve", "reviewed_sha": HEAD},
    {"verdict": "approve", "reviewed_sha": HEAD[:10], "reviewed_pr": URL},
    {"verdict": "looks fine", "reviewed_sha": HEAD, "reviewed_pr": URL},
], ids=["changes", "other_head", "other_pr", "unnamed_pr", "short_sha", "no_verdict"])
def test_awaited_review_that_is_not_an_approval_at_head_still_guards(clock, monkeypatch, metadata):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, metadata)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_with_unknown_head_still_guards(clock, monkeypatch):
    monkeypatch.setattr(kar, "pr_head", lambda url, **_: None)
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_finished_before_the_park_does_not_release(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, rev = _awaiting_owner(conn, clock, monkeypatch, APPROVE, finish=False)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        _finish(conn, rev, APPROVE)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at = ended_at - 3600 WHERE task_id=?", (rev,))
        _wake(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_must_cover_every_holding_pr(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE)
        run_id = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        with kb.write_txn(conn):
            ts = conn.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='scheduled'",
                              (tid,)).fetchone()[0]
        clock[0], saved = ts, clock[0]
        _comment(conn, tid, run_id, f"Stacked follow-up {OTHER}", monkeypatch)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,))
        clock[0] = saved
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_of_a_non_assignee_park_still_guards(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee='b' WHERE id=?", (tid,))
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_does_not_release_while_another_awaited_card_is_open(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE)
        still_open = kb.create_task(conn, title="second review", assignee="reviewer")
        with kb.write_txn(conn):
            row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                               (tid,)).fetchone()
            payload = json.loads(row["payload"])
            payload["awaiting"] = {"kind": "card", "ref": still_open}
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_after_a_parked_run_that_did_not_end_parked_still_guards(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET outcome='crashed' WHERE task_id=?", (tid,))
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_changes_are_not_outvoted_by_another_awaited_approval(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, rev = _awaiting_owner(conn, clock, monkeypatch, APPROVE)
        second = kb.create_task(conn, title="second review", assignee="reviewer")
        _finish(conn, second, {"verdict": "request_changes", "reviewed_sha": HEAD, "reviewed_pr": URL})
        with kb.write_txn(conn):
            row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='scheduled'",
                               (tid,)).fetchone()
            payload = json.loads(row["payload"])
            payload["awaiting"] = {"kind": "card", "ref": second}
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        assert rev != second
        # This resumes repair, not merge: a change request is not an approval.
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) is None
        kb.claim_task(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


def test_awaited_review_after_crash_of_the_parked_run_still_guards(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _awaiting_owner(conn, clock, monkeypatch, APPROVE)
        kb.claim_task(conn, tid)
        assert kb.reclaim_task(conn, tid, reason="worker died", signal_fn=lambda *_: None)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


@pytest.mark.parametrize("payload,expected", [
    ({"wake_check": "hermes kanban wait-check task t_abc --gateway w"}, ["t_abc"]),
    ({"wake_check": "hermes kanban wait-check task t_abc"}, ["t_abc"]),
    ({"wake_check": "echo hermes kanban wait-check task t_abc"}, []),
    ({"wake_check": "hermes kanban wait-check task t_abc; true"}, []),
    ({"_wake_episode_v1": {"park_cmd": "hermes kanban wait-check task t_def"}}, ["t_def"]),
    ({"awaiting": {"kind": "card", "ref": "t_abc"}}, ["t_abc"]),
    ({"awaiting": {"kind": "ci", "ref": "t_abc"}}, []),
    ({"awaiting": {"kind": "card", "ref": "t_abc; rm"}}, []),
    ({"awaiting": {"kind": "card", "ref": "t_abc"}, "wake_check": "hermes kanban wait-check task t_abc"},
     ["t_abc"]),
], ids=["gateway", "bare", "prefixed", "suffixed", "episode", "awaiting", "awaiting_ci", "bad_ref", "dedupe"])
def test_awaited_cards_parses_only_card_waits(payload, expected):
    assert kar.awaited_cards(payload) == expected


@pytest.mark.parametrize("stdout,code,expected", [
    (json.dumps({"state": "OPEN", "headRefOid": "A" * 40}), 0, "a" * 40),
    (json.dumps({"state": "MERGED", "headRefOid": "a" * 40}), 0, None),
    (json.dumps({"state": "OPEN", "headRefOid": "a" * 39}), 0, None),
    (json.dumps(["OPEN"]), 0, None),
    ("not json", 0, None),
    (json.dumps({"state": "OPEN", "headRefOid": "a" * 40}), 1, None),
], ids=["open", "merged", "short", "list", "garbage", "gh_failed"])
def test_pr_head_reads_only_an_open_full_head(monkeypatch, stdout, code, expected):
    import subprocess

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    monkeypatch.setattr(prs, "_gh_pr_state", _REAL_GH_PR_STATE)
    monkeypatch.setattr(subprocess, "run", run)
    kar.clear_cache()
    assert kar.pr_head(URL) == expected
    assert kar.pr_head(URL) == expected
    assert len(calls) == 1  # cached either way
    kar.clear_cache()


def test_pr_head_survives_a_gh_launch_failure(monkeypatch):
    import subprocess

    def run(argv, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(prs, "_gh_pr_state", _REAL_GH_PR_STATE)
    monkeypatch.setattr(subprocess, "run", run)
    kar.clear_cache()
    assert kar.pr_head(URL) is None
    kar.clear_cache()
