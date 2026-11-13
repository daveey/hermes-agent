"""A park waiting on a card re-probes when that card's status changes on this SAME gateway, not up to
``check_every`` later (t_01a1d5def756990d). [daveey]

PR #2351's owner (worker-5) parked on ``hermes kanban wait-check task <review>`` (awaiting kind=card, check_every
900). The review card was created, claimed and completed on worker-5 itself at 16:43:01; the owner woke on its
regular probe at 16:50:05. PR #2370 expedited only status changes that arrive by a replica pull.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import shlex
import threading
import time

import pytest

from hermes_cli import kanban_awaited_card_wake as acw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_local_status_wake as lsw
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import kanban_sync_support as support

EVERY = 900


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    acw._reset_for_tests()
    yield home
    kwc._shutdown_for_tests()
    acw._reset_for_tests()


@pytest.fixture
def live(kanban_home):
    conn = kbc.connect(kanban_home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def probes(monkeypatch):
    """Run each ``hermes kanban wait-check`` probe in-process (the real ``kanban_wait_check.run`` with the probe
    env the dispatcher builds) and count runs per waiting card."""
    from hermes_cli import kanban_wait_check as kwait

    runs: dict[str, int] = {}
    lock = threading.Lock()
    parser = argparse.ArgumentParser()
    parser.add_argument("check")
    parser.add_argument("target")
    for flag in ("--gateway", "--board", "--until"):
        parser.add_argument(flag, default=None)
    parser.add_argument("--from", dest="since", default=None)

    def run_probe(key, cmd, cwd, env):
        argv = shlex.split(cmd)
        assert argv[:3] == ["hermes", "kanban", "wait-check"], cmd
        with lock:
            runs[env["HERMES_KANBAN_TASK"]] = runs.get(env["HERMES_KANBAN_TASK"], 0) + 1
            saved = {k: os.environ.get(k) for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB",
                                                    "HERMES_KANBAN_WAKE_PROBE")}
            os.environ.update({k: env[k] for k in saved})
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    rc = kwait.run(parser.parse_args(argv[3:]))
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return rc, out.getvalue()

    monkeypatch.setattr(kwc, "_run_probe", run_probe)
    return runs


def _running(live, title):
    tid = kb.create_task(live, title=title, assignee="worker")
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(live, tid, claimer="worker") is not None
    return tid


def _set_status(live, tid, status):
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))


def _status(live, tid):
    return kb.get_task(live, tid).status


def _next_at(live, tid):
    state = kwc.get_state(live, tid)
    assert state is not None
    return state["next_at"]


def _setup(live, probes, cmd_for, *, review_status="running", awaiting_card=False):
    """The review card runs on this gateway; the owner parks on it here and runs its first probe."""
    review = _running(live, "exact-head review of PR #2351")
    if review_status != "running":
        _set_status(live, review, review_status)
    owner = _running(live, "PR #2351 landing owner")
    awaiting = {"kind": "card", "ref": review, "why": "exact-head review of PR #2351"} if awaiting_card else None
    assert kb.schedule_task(live, owner, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on the review",
                            wake_check=cmd_for(review), check_every=EVERY, awaiting=awaiting)
    t0 = int(time.time())
    kwc.run_due_wake_checks(live, now=t0, wait=True)
    assert _status(live, owner) == "scheduled" and probes[owner] == 1
    assert _next_at(live, owner) == t0 + EVERY
    sync.consume_dispatcher_wake()
    return review, owner, t0


def _wait_check(review):
    return f"hermes kanban wait-check task {review}"


# --- a. the review's local completion wakes the owner without check_every elapsing --------------------------

def _complete(live, tid):
    assert kb.complete_task(live, tid, summary="approve", metadata={"verdict": "approve"})


def _block(live, tid):
    assert kb.block_task(live, tid, reason="needs a decision")


def _review(live, tid):
    assert kb.request_review(live, tid, summary="ready for review")


def _archive(live, tid):
    from hermes_cli.kanban_db_archive import archive_task
    assert archive_task(live, tid)


@pytest.mark.parametrize("awaiting_card", [False, True], ids=["wait-check", "wait-check+awaiting-card"])
@pytest.mark.parametrize("transition, shown", [(_complete, "done"), (_block, "blocked"), (_review, "review"),
                                               (_archive, "archived")],
                         ids=["complete", "block", "request_review", "archive"])
def test_awaited_card_moving_on_this_gateway_wakes_the_owner_within_a_minute(live, probes, awaiting_card,
                                                                             transition, shown):
    review, owner, t0 = _setup(live, probes, _wait_check, awaiting_card=awaiting_card)
    transition(live, review)  # 16:43:01: the review finishes on worker-5 itself
    assert sync.consume_dispatcher_wake()  # an in-process dispatcher ticks now, not at its next interval
    assert _next_at(live, owner) <= t0 + 60  # before the fix: t0 + 900 (owner woke 16:50:05)
    kwc.run_due_wake_checks(live, now=t0 + 60, wait=True)
    assert _status(live, owner) == "ready" and probes[owner] == 3  # park probe, then the met run + its confirm
    met = [e for e in kb.list_events(live, owner) if e.kind == "condition_met"]
    assert met and f"{review} {shown}" in met[-1].payload["output"]


def test_awaiting_card_ref_alone_wakes_the_owner(live, probes):
    """A park whose probe does not name the card (a file probe) still re-probes on its ``awaiting`` card ref."""
    review = _running(live, "review")
    owner = _running(live, "owner")
    flag = os.path.join(os.path.dirname(str(kbc._main_db_file(live))), "never-written")
    assert kb.schedule_task(live, owner, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting",
                            wake_check=f"test -f {flag}", check_every=EVERY,
                            awaiting={"kind": "card", "ref": review, "why": "review"})
    with kb.write_txn(live):
        live.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (int(time.time()) + EVERY, owner))
    before = _next_at(live, owner)
    _complete(live, review)
    assert _next_at(live, owner) < before


# --- b. no status change of the awaited card: exactly the probes of today ------------------------------------

def test_no_change_of_the_awaited_card_adds_no_probe_over_30_minutes(live, probes):
    review, owner, t0 = _setup(live, probes, _wait_check)
    other = _running(live, "unrelated card")
    for minute in range(1, 31):
        # Other cards keep moving on this board, and the awaited card gets non-status edits.
        _set_status(live, other, ("ready", "running", "review", "done")[minute % 4])
        with kb.write_txn(live):
            live.execute("UPDATE tasks SET title = ? WHERE id = ?", (f"review, edit {minute}", review))
        kwc.run_due_wake_checks(live, now=t0 + 60 * minute, wait=True)
        assert _status(live, owner) == "scheduled"
    assert probes[owner] == 3  # t0, t0+900, t0+1800: the check_every cadence alone, as on main
    assert not sync.consume_dispatcher_wake()


# --- c. --from and unmet changes ------------------------------------------------------------------------------

@pytest.mark.parametrize("since", ["blocked", "review", "triage"])
def test_from_status_still_reads_not_yet_and_an_unmet_change_probes_at_most_once(live, probes, since):
    review, owner, t0 = _setup(live, probes, lambda r: f"{_wait_check(r)} --from {since}", review_status=since)
    _set_status(live, review, "running")  # an intermediate change that is not met
    with kb.write_txn(live):  # a later commit with no status change of the review
        live.execute("UPDATE tasks SET title = 'edited' WHERE id = ?", (review,))
    kwc.run_due_wake_checks(live, now=t0 + 30, wait=True)
    kwc.run_due_wake_checks(live, now=t0 + 90, wait=True)
    assert _status(live, owner) == "scheduled" and probes[owner] == 2  # one extra probe, then back on cadence
    assert _next_at(live, owner) == t0 + 30 + EVERY
    _set_status(live, review, since)  # back in the --from status: still "unchanged since park"
    kwc.run_due_wake_checks(live, now=t0 + 120, wait=True)
    assert _status(live, owner) == "scheduled" and probes[owner] == 3
    _set_status(live, review, "done")  # (complete_task refuses a triage card; a plain local status write)
    kwc.run_due_wake_checks(live, now=t0 + 150, wait=True)
    assert _status(live, owner) == "ready"


# --- guards ---------------------------------------------------------------------------------------------------

def test_a_rolled_back_transition_expedites_nothing(live, probes):
    review, owner, t0 = _setup(live, probes, _wait_check)
    with pytest.raises(RuntimeError), kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (review,))
        raise RuntimeError("boom")
    assert _status(live, review) == "running" and _next_at(live, owner) == t0 + EVERY
    assert not sync.consume_dispatcher_wake()


def test_a_commit_no_park_mentions_never_reads_the_parks(live, probes, monkeypatch):
    _review_id, owner, t0 = _setup(live, probes, _wait_check)
    calls = []
    monkeypatch.setattr(acw, "expedite", lambda conn, changed, **kw: calls.append(changed) or [])
    other = _running(live, "unrelated")
    _complete(live, other)
    assert calls == [] and _next_at(live, owner) == t0 + EVERY


def test_a_failing_expedite_never_fails_the_transition(live, probes, monkeypatch, caplog):
    review, owner, t0 = _setup(live, probes, _wait_check)

    def boom(conn, changed, **kw):
        raise RuntimeError("expedite broke")

    monkeypatch.setattr(acw, "expedite", boom)
    _complete(live, review)
    assert _status(live, review) == "done" and _next_at(live, owner) == t0 + EVERY
    assert "could not expedite" in caplog.text


def test_a_sweep_past_the_prefilter_cap_still_expedites(live, probes, monkeypatch):
    review, owner, _t0 = _setup(live, probes, _wait_check)
    monkeypatch.setattr(lsw, "_PREFILTER_MAX", 0)
    _complete(live, review)
    assert _next_at(live, owner) <= int(time.time())


def test_a_board_without_tasks_records_nothing(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "bare.db", isolation_level=None)
    try:
        lsw.arm(conn)  # no ``tasks`` table: the install fails quietly
        assert lsw.flush(conn) == []
    finally:
        conn.close()
