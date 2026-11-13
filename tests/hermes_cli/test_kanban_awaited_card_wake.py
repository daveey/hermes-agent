"""A park waiting on a card another gateway owns re-probes when that card's status change reaches this gateway's
replica, not up to ``check_every`` later (t_01a1d57d2d295afd). [daveey]

PR #2277's owner (worker-5) parked on ``hermes kanban wait-check task <review>`` with check_every 900; the review
completed on worker-1 at 07:14 PST and the owner slept until an operator woke it at 07:17 (next probe 07:24).
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
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "worker-5", "worker-1", "default"
REVIEW = "t_01a1d5749bb06b1f"
UNRELATED = "t_0000000000000abc"
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
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def live(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
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


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


_HLC = [0]


def _other(svc, tid, fields):
    """worker-1 writes its own card on the Board (it never exists in worker-5's live board)."""
    _HLC[0] = max(_HLC[0] + 1, time.time_ns() // 1_000_000 + 1000)  # strictly increasing HLC millis
    res = svc.hub(OTHER).ops(BOARD, [{
        "client_op_id": f"other-{tid}-{_HLC[0]}", "op": "upsert", "table": "tasks", "uid": tid,
        "hlc": f"{_HLC[0]:013d}-0000-{OTHER}",
        "fields": {"title": tid, "created_at": 1, **fields}}])
    assert res["results"][0]["status"] == "applied", res


def _park(live, cmd, *, awaiting=None):
    tid = kb.create_task(live, title="PR #2277 owner", assignee="worker")
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(live, tid, claimer="worker") is not None
    assert kb.schedule_task(live, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on the review",
                            wake_check=cmd, check_every=EVERY, awaiting=awaiting)
    return tid


def _next_at(live, tid):
    state = kwc.get_state(live, tid)
    assert state is not None
    return state["next_at"]


def _status(live, tid):
    return kb.get_task(live, tid).status


def _setup(live, svc, kanban_home, probes, cmd, *, review_status="running", awaiting=None):
    """The review card exists on worker-1 and in worker-5's replica; the owner parks and runs its first probe."""
    _other(svc, REVIEW, {"status": review_status})
    _round(kanban_home, svc)
    owner = _park(live, cmd, awaiting=awaiting)
    t0 = int(time.time())
    kwc.run_due_wake_checks(live, now=t0, wait=True)
    assert _status(live, owner) == "scheduled" and probes[owner] == 1
    assert _next_at(live, owner) == t0 + EVERY
    sync.consume_dispatcher_wake()
    return owner, t0


# --- a. the review's completion wakes the owner without check_every elapsing ---------------------------------

@pytest.mark.parametrize("awaiting", [None, {"kind": "card", "ref": REVIEW, "gateway": OTHER,
                                              "why": "exact-head review of PR #2277"}])
def test_review_done_on_another_gateway_wakes_the_owner_within_a_minute(live, svc, kanban_home, probes, awaiting,
                                                                        monkeypatch):
    from hermes_cli import kanban_awaiting as ka
    # The park-time confirmation of the remote producer (kanban_schedule runs `wait-check --gateway` for it).
    monkeypatch.setitem(ka._REMOTE_CONFIRMED, (OTHER, REVIEW), (time.monotonic(), f"{REVIEW} running"))
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW}", awaiting=awaiting)
    _other(svc, REVIEW, {"status": "done"})  # 07:14 PST: the review completes on worker-1
    _round(kanban_home, svc)  # worker-5's replica applies it
    assert sync.consume_dispatcher_wake()  # the dispatcher tick runs now, not at its next interval
    assert _next_at(live, owner) <= t0 + 60  # before the fix: t0 + 900
    kwc.run_due_wake_checks(live, now=t0 + 60, wait=True)
    assert _status(live, owner) == "ready"
    met = [e for e in kb.list_events(live, owner) if e.kind == "condition_met"]
    assert met and f"{REVIEW} done" in met[-1].payload["output"]


@pytest.fixture
def lagging_registry(tmp_path, monkeypatch):
    """worker-1 is unreachable over ssh from here (rc 255, as from every pool worker: name not resolved or
    permission denied), so ``--gateway`` probes fall back to the hermes-hub registry replica. That replica is
    collector-refreshed: fresh (observed 30 s ago) but it still holds the review as ``running`` after the Board
    sync replica already applied ``done`` (t_01a1d5ead059278d: sources disagreed for minutes)."""
    import json
    import sqlite3
    import subprocess
    from hermes_cli import kanban_wait_check as kwait

    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    db = sqlite3.connect(state / "hub.db")
    db.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
    scope = {"task_scopes": [{"board": BOARD, "profile": "default", "state": "ok", "observed_at": time.time() - 30}]}
    db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (OTHER, "gateway", OTHER, json.dumps(scope), 1))
    row = dict(id=REVIEW, board=BOARD, status="running", gateway=OTHER, profile="default", owner_profile="default",
               wait_observation={"parked_agent": False, "orphan_reason": None})
    db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (OTHER, "tasks", f"default/{BOARD}/{REVIEW}",
                                                         json.dumps(row), 1))
    db.commit()
    db.close()
    ssh = []

    def unreachable(argv, **kwargs):
        ssh.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", f"ssh: Could not resolve hostname {OTHER}")

    monkeypatch.setattr(kwait.subprocess, "run", unreachable)
    return ssh


def test_gateway_probe_wakes_on_the_board_change_while_ssh_fails_and_the_registry_lags(
        live, svc, kanban_home, probes, lagging_registry, monkeypatch):
    """The live #2370 gap: both production parks used ``--gateway`` and slept the full ``check_every``."""
    from hermes_cli import kanban_awaiting as ka
    monkeypatch.setitem(ka._REMOTE_CONFIRMED, (OTHER, REVIEW), (time.monotonic(), f"{REVIEW} running"))
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW} --gateway {OTHER}")
    assert lagging_registry  # the probe tried the owner first
    _other(svc, REVIEW, {"status": "done"})
    _round(kanban_home, svc)  # this gateway's Board replica applies done; the registry replica still says running
    assert sync.consume_dispatcher_wake()
    assert _next_at(live, owner) <= t0 + 60
    kwc.run_due_wake_checks(live, now=t0 + 60, wait=True)
    assert _status(live, owner) == "ready"  # before the fix: scheduled until t0 + 900
    met = [e for e in kb.list_events(live, owner) if e.kind == "condition_met"]
    assert met and f"{REVIEW} done" in met[-1].payload["output"]


def test_gateway_probe_keeps_the_registry_answer_when_the_board_replica_is_not_fresh(
        live, svc, kanban_home, probes, lagging_registry, monkeypatch):
    """A Board replica whose last pull is old vouches for nothing: the registry's (newer) not-yet stands."""
    from hermes_cli import kanban_awaiting as ka
    monkeypatch.setitem(ka._REMOTE_CONFIRMED, (OTHER, REVIEW), (time.monotonic(), f"{REVIEW} running"))
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW} --gateway {OTHER}")
    _other(svc, REVIEW, {"status": "done"})
    _round(kanban_home, svc)
    import sqlite3
    with sqlite3.connect(ksc.shadow_path(BOARD)) as shadow:
        shadow.execute("UPDATE kanban_sync_state SET pulled_at = ?", (time.time() - 3600,))
    kwc.run_due_wake_checks(live, now=t0 + 60, wait=True)
    assert _status(live, owner) == "scheduled"
    assert _next_at(live, owner) == t0 + 60 + EVERY


def test_awaiting_card_ref_alone_names_the_card(live, kanban_home):
    """A park whose own probe does not name the card still counts its ``awaiting`` card ref."""
    owner = kb.create_task(live, title="owner", assignee="worker")
    with kb.write_txn(live):
        kb._append_event(live, owner, "scheduled", {"awaiting": {"kind": "card", "ref": REVIEW}})
    assert acw.named_cards(live, owner, "test -f /x") == {REVIEW}
    assert acw.named_cards(live, owner, f"hermes kanban wait-check task {UNRELATED} --gateway hub") == {
        REVIEW, UNRELATED}
    assert acw.named_cards(live, owner, f"hermes kanban wait-check task {owner}") == {REVIEW}  # never itself


# --- b. no status change of the awaited card: exactly the probes of today ------------------------------------

def test_no_change_of_the_awaited_card_adds_no_probe_over_30_minutes(live, svc, kanban_home, probes):
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW}")
    _other(svc, UNRELATED, {"status": "ready"})
    for minute in range(1, 31):
        # Other cards keep moving on the Board, and the awaited card gets non-status edits.
        _other(svc, UNRELATED, {"status": ("running", "review", "done")[minute % 3]})
        _other(svc, REVIEW, {"title": f"review, edit {minute}"})
        _round(kanban_home, svc)
        kwc.run_due_wake_checks(live, now=t0 + 60 * minute, wait=True)
        assert _status(live, owner) == "scheduled"
    assert probes[owner] == 3  # t0, t0+900, t0+1800: the check_every cadence alone, as on main


# --- c. --from and unmet changes ------------------------------------------------------------------------------

@pytest.mark.parametrize("since", ["blocked", "review", "triage"])
def test_from_status_still_reads_not_yet_and_an_unmet_change_probes_at_most_once(
        live, svc, kanban_home, probes, since):
    cmd = f"hermes kanban wait-check task {REVIEW} --from {since}"
    owner, t0 = _setup(live, svc, kanban_home, probes, cmd, review_status=since)
    _other(svc, REVIEW, {"status": "running"})  # an intermediate change that is not met
    _round(kanban_home, svc)
    _round(kanban_home, svc)  # a second round with no further change
    kwc.run_due_wake_checks(live, now=t0 + 30, wait=True)
    kwc.run_due_wake_checks(live, now=t0 + 90, wait=True)
    assert _status(live, owner) == "scheduled" and probes[owner] == 2  # one extra probe, then back on cadence
    assert _next_at(live, owner) == t0 + 30 + EVERY
    _other(svc, REVIEW, {"status": since})  # back in the --from status: still "unchanged since park"
    _round(kanban_home, svc)
    kwc.run_due_wake_checks(live, now=t0 + 120, wait=True)
    assert _status(live, owner) == "scheduled" and probes[owner] == 3
    _other(svc, REVIEW, {"status": "done"})
    _round(kanban_home, svc)
    kwc.run_due_wake_checks(live, now=t0 + 150, wait=True)
    assert _status(live, owner) == "ready"


# --- e. detection: a park still asleep after its awaited card finished ---------------------------------------

def test_lag_warning_fires_once_for_a_park_that_cannot_see_the_done_card_and_never_after_a_wake(
        live, svc, kanban_home, probes, caplog):
    caplog.set_level("WARNING", logger=acw.__name__)
    blind = f"hermes kanban wait-check task {REVIEW} --until review"  # done is not met for this probe
    owner, _t0 = _setup(live, svc, kanban_home, probes, blind)
    _other(svc, REVIEW, {"status": "done"})
    _round(kanban_home, svc)
    seen = time.time()
    kwc.run_due_wake_checks(live, now=int(seen) + 30, wait=True)
    assert _status(live, owner) == "scheduled" and probes[owner] == 2

    def lags():
        return [r.getMessage() for r in caplog.records if "awaited_card_done_park_lag" in r.getMessage()]

    assert acw.report_lag(live, now=seen + acw.LAG_WARN_SECONDS - 5) == [] and not lags()
    kwc.run_due_wake_checks(live, now=int(seen) + acw.LAG_WARN_SECONDS + 2, wait=True)
    [line] = lags()
    assert f"task={owner} awaited={REVIEW} lag_s=" in line
    kwc.run_due_wake_checks(live, now=int(seen) + acw.LAG_WARN_SECONDS + 60, wait=True)
    assert len(lags()) == 1  # once


def test_no_lag_warning_once_the_park_woke(live, svc, kanban_home, probes, caplog):
    caplog.set_level("WARNING", logger=acw.__name__)
    owner, _t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW}")
    _other(svc, REVIEW, {"status": "done"})
    _round(kanban_home, svc)
    seen = time.time()
    kwc.run_due_wake_checks(live, now=int(seen) + 5, wait=True)
    assert _status(live, owner) == "ready"
    assert acw.report_lag(live, now=seen + 10 * acw.LAG_WARN_SECONDS) == []
    assert not [r for r in caplog.records if "awaited_card_done_park_lag" in r.getMessage()]


def test_expedite_never_raises_and_ignores_cards_no_park_names(live):
    assert acw.expedite(live, {}) == []
    assert acw.expedite(live, {UNRELATED: "done"}) == []
    assert not sync.consume_dispatcher_wake()


def _local_park(live, monkeypatch):
    from hermes_cli import kanban_awaiting as ka
    monkeypatch.setitem(ka._REMOTE_CONFIRMED, (OTHER, REVIEW), (time.monotonic(), f"{REVIEW} running"))
    owner = _park(live, f"hermes kanban wait-check task {REVIEW}",
                  awaiting={"kind": "card", "ref": REVIEW, "gateway": OTHER, "why": "review"})
    with kb.write_txn(live):
        live.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (int(time.time()) + EVERY, owner))
    sync.consume_dispatcher_wake()
    return owner


def test_a_probe_already_due_is_left_alone_and_wakes_nothing(live, monkeypatch):
    owner = _local_park(live, monkeypatch)
    due = int(time.time()) - 5
    with kb.write_txn(live):
        live.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (due, owner))
    assert acw.expedite(live, {REVIEW: "done"}) == []
    assert _next_at(live, owner) == due and not sync.consume_dispatcher_wake()


def test_a_park_that_left_scheduled_meanwhile_is_not_touched(live, monkeypatch):
    """The parks are read outside the write txn: one that wakes before the update keeps its row as it is."""
    owner = _local_park(live, monkeypatch)
    before = _next_at(live, owner)
    real = acw.named_cards

    def woke_meanwhile(conn, task_id, cmd):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        return real(conn, task_id, cmd)

    monkeypatch.setattr(acw, "named_cards", woke_meanwhile)
    assert acw.expedite(live, {REVIEW: "done"}) == []
    assert _next_at(live, owner) == before and not sync.consume_dispatcher_wake()


def test_a_reopened_awaited_card_drops_its_lag_record(live, monkeypatch):
    owner = _local_park(live, monkeypatch)
    now = time.time()
    assert acw.expedite(live, {REVIEW: "done"}, now=now) == [owner]
    with kb.write_txn(live):  # the probe ran and went back on cadence, the card reopened on worker-1
        live.execute("UPDATE task_wake_checks SET next_at = ? WHERE task_id = ?", (int(now) + EVERY, owner))
    assert acw.expedite(live, {REVIEW: "running"}, now=now + 1) == [owner]
    assert acw.report_lag(live, now=now + 10 * acw.LAG_WARN_SECONDS) == []


def test_lag_records_are_bounded(live, monkeypatch):
    owner = _local_park(live, monkeypatch)
    monkeypatch.setattr(acw, "_MAX_WATCHED", 1)
    acw._WATCH[("elsewhere", "t_0000000000000001")] = (REVIEW, "done", 0.0)
    acw.expedite(live, {REVIEW: "done"}, now=time.time())
    assert list(acw._WATCH) == [(acw._db_key(live), owner)]


# --- f. every replica apply path: a snapshot rebuild and `kanban sync reconcile` (review 5894353204) ----------

def _confirmed(monkeypatch):
    from hermes_cli import kanban_awaiting as ka
    monkeypatch.setitem(ka._REMOTE_CONFIRMED, (OTHER, REVIEW), (time.monotonic(), f"{REVIEW} running"))


def _woke_within_a_minute(live, owner, t0):
    assert sync.consume_dispatcher_wake()
    assert _next_at(live, owner) <= t0 + 60  # before the fix: t0 + 900 (only the check_every cadence)
    kwc.run_due_wake_checks(live, now=t0 + 60, wait=True)
    assert _status(live, owner) == "ready"
    met = [e for e in kb.list_events(live, owner) if e.kind == "condition_met"]
    assert met and f"{REVIEW} done" in met[-1].payload["output"]


def test_review_done_arriving_by_a_snapshot_rebuild_wakes_the_owner_within_a_minute(
        live, svc, kanban_home, probes, monkeypatch):
    _confirmed(monkeypatch)
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW}")
    rebuilds = []
    real = ksc.resync
    monkeypatch.setattr(ksc, "resync", lambda conn, *a, **kw: rebuilds.append(kw) or real(conn, *a, **kw))
    _other(svc, REVIEW, {"status": "done"})
    svc.compact(BOARD)  # the retention floor passes the replica's cursor: the round rebuilds from the snapshot
    _round(kanban_home, svc)
    assert rebuilds and rebuilds[0].get("partition") is None  # the replica was rebuilt, not pulled
    _woke_within_a_minute(live, owner, t0)


@pytest.mark.parametrize("compacted", [False, True])
def test_review_done_arriving_by_sync_reconcile_wakes_the_owner_within_a_minute(
        live, svc, kanban_home, probes, monkeypatch, compacted):
    _confirmed(monkeypatch)
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW}")
    _other(svc, REVIEW, {"status": "done"})
    if compacted:  # reconcile's own replica pull meets the retention floor and rebuilds
        svc.compact(BOARD)
    ksc.reconcile(BOARD, kanban_home / "kanban.db", svc.hub(ME))  # no sync round runs
    _woke_within_a_minute(live, owner, t0)


def test_lag_warning_reads_the_replica_for_a_status_no_expedite_was_handed(
        live, svc, kanban_home, probes, caplog):
    """A replica apply that never reaches ``expedite`` (here ``sync diff``'s diagnostic pull) is still seen."""
    caplog.set_level("WARNING", logger=acw.__name__)
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW} --until review")
    _other(svc, REVIEW, {"status": "done"})
    ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db", diagnostic=True)
    assert not acw._WATCH  # nothing was handed the change

    def lags():
        return [r.getMessage() for r in caplog.records if "awaited_card_done_park_lag" in r.getMessage()]

    scanned = t0 + acw._SCAN_SECONDS
    assert acw.report_lag(live, now=scanned) == []  # the replica read records it
    assert acw.report_lag(live, now=scanned + acw.LAG_WARN_SECONDS - 5) == [] and not lags()
    assert acw.report_lag(live, now=scanned + acw.LAG_WARN_SECONDS + 1) == [owner]
    [line] = lags()
    assert f"task={owner} awaited={REVIEW} lag_s=" in line
    for later in range(2, 12):  # later scans still see it done: warned once, not once per scan
        acw.report_lag(live, now=scanned + acw.LAG_WARN_SECONDS + 60 * later)
    assert len(lags()) == 1


@pytest.mark.parametrize("other_card", [False, True])
def test_a_warned_park_that_re_parks_within_one_scan_window_on_a_finished_card_is_warned_again(
        live, svc, kanban_home, probes, caplog, other_card):
    """Review 5895271396 nit 1 on #2432: the warned mark was keyed by park only, and a scan that still found the park
    on a finished card kept it, so a re-park inside one ``_SCAN_SECONDS`` window was never warned again."""
    caplog.set_level("WARNING", logger=acw.__name__)
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW} --until review")
    _other(svc, REVIEW, {"status": "done"})
    _other(svc, UNRELATED, {"status": "done"})
    ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db", diagnostic=True)

    def lags():
        return [r.getMessage() for r in caplog.records if "awaited_card_done_park_lag" in r.getMessage()]

    scanned = t0 + acw._SCAN_SECONDS
    acw.report_lag(live, now=scanned)
    warned_at = scanned + acw.LAG_WARN_SECONDS + 1
    assert acw.report_lag(live, now=warned_at) == [owner]
    acw.report_lag(live, now=warned_at + 30)  # same episode, no scan due: still once
    assert len(lags()) == 1
    # The park wakes, runs and re-parks inside the scan window, still on a finished card (the same one or another).
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (owner,))
    assert kb.claim_task(live, owner, claimer="worker") is not None
    ref = UNRELATED if other_card else REVIEW
    assert kb.schedule_task(live, owner, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z still waiting",
                            wake_check=f"hermes kanban wait-check task {ref} --until review", check_every=EVERY)
    rescanned = warned_at + acw._SCAN_SECONDS
    assert acw.report_lag(live, now=rescanned) == []  # the scan re-arms the new episode
    assert acw.report_lag(live, now=rescanned + acw.LAG_WARN_SECONDS + 1) == [owner]  # before the fix: []
    assert len(lags()) == 2 and f"task={owner} awaited={ref} lag_s=" in lags()[-1]
    for later in range(2, 12):  # the new episode across later scans: warned once
        acw.report_lag(live, now=rescanned + acw.LAG_WARN_SECONDS + 60 * later)
    assert len(lags()) == 2


def test_a_warned_park_whose_other_named_card_finishes_in_the_same_episode_is_warned_again(
        live, svc, kanban_home, probes, caplog, monkeypatch):
    """A park naming two cards (its probe's and its ``awaiting`` ref) is warned for the first to finish; when the
    other, lower-sorted one finishes later without a re-park, the scan's awaited card changes inside the same
    episode and is warned again (t_01a1d63b4baaa813, mutant M2: a mark compared by episode alone never re-arms)."""
    from hermes_cli import kanban_awaiting as ka
    caplog.set_level("WARNING", logger=acw.__name__)
    assert UNRELATED < REVIEW  # the scan picks the lowest-sorted finished card a park names
    monkeypatch.setitem(ka._REMOTE_CONFIRMED, (OTHER, UNRELATED), (time.monotonic(), f"{UNRELATED} running"))
    _other(svc, UNRELATED, {"status": "running"})
    owner, t0 = _setup(live, svc, kanban_home, probes, f"hermes kanban wait-check task {REVIEW} --until review",
                       awaiting={"kind": "card", "ref": UNRELATED, "gateway": OTHER, "why": "second card"})
    assert acw.named_cards(live, owner, f"hermes kanban wait-check task {REVIEW}") == {REVIEW, UNRELATED}
    _other(svc, REVIEW, {"status": "done"})
    ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db", diagnostic=True)

    def lags():
        return [r.getMessage() for r in caplog.records if "awaited_card_done_park_lag" in r.getMessage()]

    scanned = t0 + acw._SCAN_SECONDS
    assert acw.report_lag(live, now=scanned) == []
    warned_at = scanned + acw.LAG_WARN_SECONDS + 1
    assert acw.report_lag(live, now=warned_at) == [owner]
    assert len(lags()) == 1 and f"task={owner} awaited={REVIEW} lag_s=" in lags()[-1]
    episode = acw._episode(live, owner)
    _other(svc, UNRELATED, {"status": "done"})  # the other named card finishes; the park does not re-park
    ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db", diagnostic=True)
    rescanned = warned_at + acw._SCAN_SECONDS
    assert acw.report_lag(live, now=rescanned) == []  # the scan re-arms the park for the new awaited card
    assert acw._episode(live, owner) == episode and _status(live, owner) == "scheduled"
    assert acw.report_lag(live, now=rescanned + acw.LAG_WARN_SECONDS + 1) == [owner]  # M2: []
    assert len(lags()) == 2 and f"task={owner} awaited={UNRELATED} lag_s=" in lags()[-1]
    for later in range(2, 12):  # that awaited card across later scans: warned once
        acw.report_lag(live, now=rescanned + acw.LAG_WARN_SECONDS + 60 * later)
    assert len(lags()) == 2


def test_a_warned_park_that_re_parks_is_re_armed_by_expedite_inside_the_scan_window(
        live, svc, kanban_home, monkeypatch, caplog):
    """The expedite path re-arms a warned park that re-parked (``_remember`` -> ``_rearm``), so its lag counts from
    when expedite applied the status, not from the next replica scan (t_01a1d63b4baaa813, mutant M5; review
    5895271396 follow-up nit 2 on #2470)."""
    caplog.set_level("WARNING", logger=acw.__name__)
    owner = _local_park(live, monkeypatch)
    _other(svc, REVIEW, {"status": "done"})
    ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db", diagnostic=True)

    def lags():
        return [r.getMessage() for r in caplog.records if "awaited_card_done_park_lag" in r.getMessage()]

    s0 = time.time()
    assert acw.report_lag(live, now=s0) == []  # the replica scan records it
    warned_at = s0 + acw.LAG_WARN_SECONDS + 1  # this call also scans: the throttle window starts here
    assert acw.report_lag(live, now=warned_at) == [owner] and len(lags()) == 1
    # The park wakes, runs and re-parks on the same (still done) card: a new episode.
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (owner,))
    assert kb.claim_task(live, owner, claimer="worker") is not None
    assert kb.schedule_task(live, owner, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z still waiting",
                            wake_check=f"hermes kanban wait-check task {REVIEW} --until review", check_every=EVERY)
    applied = warned_at + 5  # inside the scan throttle: only expedite can re-arm it now
    acw.expedite(live, {REVIEW: "done"}, now=applied)
    assert acw._WATCH.get((acw._db_key(live), owner)) == (REVIEW, "done", applied)  # M5: not watched
    assert acw.report_lag(live, now=applied + acw.LAG_WARN_SECONDS + 1) == [owner]  # M5: [] (seen at this scan)
    assert len(lags()) == 2 and f"lag_s={acw.LAG_WARN_SECONDS + 1}" in lags()[-1]


def test_the_replica_read_is_throttled_and_skipped_without_a_replica(live, monkeypatch):
    calls = []
    monkeypatch.setattr(acw, "_replica_finished", lambda conn: calls.append(1) or None)
    now = time.time()
    for dt in (0, 10, 30, 59):
        acw.report_lag(live, now=now + dt)
    assert len(calls) == 1
    acw.report_lag(live, now=now + 60)
    assert len(calls) == 2


def test_rebuilt_diffs_the_replica_and_keeps_the_pages_applied_before_the_resync(live):
    a = kb.create_task(live, title="a")
    b = kb.create_task(live, title="b")
    before = acw.statuses(live, {b: "review"})
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    c = kb.create_task(live, title="c")
    got = acw.rebuilt(before, live)
    assert got[a] == "done" and c in got and b in got  # changed, new, and changed by a page before the Resync
    assert got[b] == kb.get_task(live, b).status  # at its rebuilt status
    assert set(acw.rebuilt((None, {}), live)) == {a, b, c}  # an unreadable pre-read: every card counts


def test_changes_page_attaches_the_pull_s_applied_changes_to_a_resync():
    class Hub:
        def changes(self, board, cursor):
            raise ksc.Resync("below the floor")

    with pytest.raises(ksc.Resync) as info:
        acw.changes_page(Hub(), BOARD, 7, {REVIEW: "done"})
    assert info.value.status_changed == {REVIEW: "done"}
