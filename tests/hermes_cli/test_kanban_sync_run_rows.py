"""Every path that ends or abandons a claim ends that claim's run row, on this gateway and on the Board.

The live failure (2026-10-27, MacBook ``default``): a run that ended within one sync round of an earlier push of
the same row came back ``running``. The pull applied the feed copy of an older state of the row (our own earlier
op, whose outbox row the push had already deleted, or the Board's lease-grant row) over the newer local row, and
the push then sent that clobbered row whole, so the Board went back to ``running`` too. Runs carry no
``current_run_id`` after they end, so nothing ever closed them again.

Service: the in-memory Board fake with Board-faithful lease runs (``kanban_sync_fake_board``).
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_divergence as divergence
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "macbook", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


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


def _round(kanban_home, svc):
    res = ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert res.error is None and (res.push is None or res.push.rejected == 0)
    return res


def _run(conn, uid):
    return conn.execute("SELECT status, outcome, ended_at FROM task_runs WHERE uid = ?", (uid,)).fetchone()


def _board_run(svc, uid):
    return svc.board(BOARD)["rows"]["task_runs"][uid]


def _claimed(live, kanban_home, svc, title):
    tid = kb.create_task(live, title=title, assignee="worker")
    _round(kanban_home, svc)
    assert kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    uid = live.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
                       (tid,)).fetchone()[0]
    return tid, uid


def _assert_ended(live, svc, uid, outcome):
    local = _run(live, uid)
    assert (local["status"], local["outcome"]) == (outcome, outcome) and local["ended_at"], dict(local)
    board = _board_run(svc, uid)
    assert (board["status"], board["outcome"]) == (outcome, outcome) and board["ended_at"], board
    dangling = live.execute(
        "SELECT r.id FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE r.ended_at IS NULL "
        "AND r.status = 'running' AND (t.current_run_id IS NULL OR t.current_run_id != r.id)").fetchall()
    assert not dangling


def _expire_board_lease(svc, tid, uid):
    """Model the authoritative expiry/feed (the fake has no DO alarm).

    Real alarm/crash races are covered by the Worker suite; here retain the
    sync regression: an earlier grant/push echo must not reopen the ended run.
    """
    b = svc.board(BOARD)
    lease = b["leases"].pop(tid)
    assert lease["run_uid"] == uid
    clock = "9999999999999-0001-board"
    svc._put(b, "task_runs", uid,
             {"status": "reclaimed", "outcome": "reclaimed", "ended_at": int(time.time())},
             "server", None, clock)
    fields = {"status": "ready", "current_run_uid": None, "lease_holder": None, "lease_expires": None}
    clocks = {**b["rows"]["tasks"][tid].get("field_hlc", {}), **dict.fromkeys(fields, clock)}
    svc._put(b, "tasks", tid, {**fields, "field_hlc": clocks}, "server", None, clock)


def test_stale_lock_reclaim_survives_the_echo_of_the_runs_earlier_push(live, kanban_home, svc):
    tid, uid = _claimed(live, kanban_home, svc, "stale lock")
    _round(kanban_home, svc)  # the run's insert reaches the Board; its echo now waits in the feed
    live.execute("UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?", (int(time.time()) - 5, tid))
    assert kb.release_stale_claims(live) == 0
    assert _run(live, uid)["status"] == "running"  # no competing local finalization
    _expire_board_lease(svc, tid, uid)
    _round(kanban_home, svc)  # pull (the stale echo) then push (the end)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "reclaimed")


@pytest.mark.parametrize("mode,remote_end,local_end,seen,version,expected", [
    ("primary", 10, None, None, 2, False),
    ("primary", None, None, None, 2, True),
    ("shadow", 10, None, None, 2, True),
    ("primary", 10, 9, None, 2, True),
    ("primary", 10, None, 2, 2, True),
    ("primary", 10, None, 3, 2, True),
])
def test_pending_run_start_does_not_mask_newer_authoritative_end(
    live, kanban_home, svc, mode, remote_end, local_end, seen, version, expected,
):
    tid, uid = _claimed(live, kanban_home, svc, "pending start fence")
    rid = live.execute("SELECT id FROM task_runs WHERE uid=?", (uid,)).fetchone()[0]
    live.execute("UPDATE task_runs SET ended_at=?, sync_version=? WHERE id=?", (local_end, seen, rid))
    live.commit()
    sync.set_mode(live, mode)
    change = sync.RemoteChange(seq=1, table="task_runs", uid=uid, op="upsert", version=version,
                               hlc="", origin_gateway="server",
                               fields={"task_id": tid, "ended_at": remote_end, "status": "reclaimed"})
    assert sync._local_row_is_newer(live, "task_runs", change, rid) is expected


@pytest.mark.parametrize("mode,local_patch,remote_patch,version,expected", [
    ("primary", {}, {}, 3, False),
    ("primary", {}, {"status": "blocked", "outcome": "blocked"}, 3, False),
    ("primary", {}, {"status": "crashed", "outcome": "crashed"}, 3, False),
    ("shadow", {}, {}, 3, True),
    ("primary", {}, {}, 2, True),
    ("primary", {}, {}, 1, True),
    ("primary", {"error": "another reclaim reason"}, {}, 3, True),
    ("primary", {"error": None}, {}, 3, True),
    ("primary", {"status": "done"}, {}, 3, True),
    ("primary", {"outcome": "completed"}, {}, 3, True),
    ("primary", {}, {"ended_at": None}, 3, True),
    ("primary", {}, {"status": "running"}, 3, True),
    ("primary", {}, {"status": None}, 3, True),
    ("primary", {}, {"status": "reclaimed"}, 3, True),
    ("primary", {}, {"outcome": "reclaimed"}, 3, True),
    ("primary", {}, {"outcome": None}, 3, True),
])
def test_pending_dangling_override_is_narrow(live, kanban_home, svc, mode, local_patch,
                                            remote_patch, version, expected):
    tid, uid = _claimed(live, kanban_home, svc, "narrow dangling override")
    values = {"status": "reclaimed", "outcome": "reclaimed", "ended_at": 9, "sync_version": 2,
              "error": "dangling run: no longer the card's current run", **local_patch}
    with kbc.write_txn(live):
        live.execute("UPDATE task_runs SET " + ", ".join(f"{key}=?" for key in values) + " WHERE uid=?",
                     (*values.values(), uid))
    sync.set_mode(live, mode)
    rid = live.execute("SELECT id FROM task_runs WHERE uid=?", (uid,)).fetchone()[0]
    change = sync.RemoteChange(seq=1, table="task_runs", uid=uid, op="upsert", version=version,
                               hlc="", origin_gateway="worker-a4",
                               fields={"task_id": tid, "ended_at": 10, "status": "done",
                                       "outcome": "completed", **remote_patch})
    assert sync._local_row_is_newer(live, "task_runs", change, rid) is expected


def test_orphan_sweep_survives_the_echo_of_the_runs_earlier_push(live, kanban_home, svc):
    tid, uid = _claimed(live, kanban_home, svc, "orphan")
    _round(kanban_home, svc)
    # A dead claimer long ago: no local claim and a lease mirror that already lapsed (a live one we hold
    # means a claim may still be in flight, which the sweep leaves alone).
    live.execute("UPDATE tasks SET claim_lock = NULL, worker_pid = NULL, lease_expires = ? WHERE id = ?",
                 (int(time.time() - 5) * 1000, tid))
    live.execute("UPDATE task_events SET created_at = created_at - 3600 WHERE task_id = ? AND kind = 'claimed'",
                 (tid,))
    assert kd.reconcile_orphaned_running(live) == [tid]
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "reclaimed")


def test_board_grant_row_mid_claim_is_not_reconciled(live, kanban_home, svc):
    """The MacBook incident (t_01a18189ff87b1c1, 10-28): the Board's lease grant writes the card
    ``running``; the feed applied that row before the local claim landed (the claim was refused by the
    update admission hold and its lease released). Locally the card read ``running`` with no claim and
    no worker, the orphan sweep requeued the review card to ``ready``, and an implementation worker ran
    it again. A card whose Board lease this gateway still holds has a claim in flight: leave it."""
    tid = kb.create_task(live, title="review mid-claim", assignee="worker")
    live.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    live.commit()
    _round(kanban_home, svc)
    gate = sync.claim_gate(live, tid, int(time.time()) + 900, lane="review")
    assert gate.lease is not None and gate.lease.granted
    _round(kanban_home, svc)  # the grant's row reaches us: running, our lease, no local claim
    row = live.execute("SELECT status, claim_lock, lease_holder FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["status"], row["claim_lock"], row["lease_holder"]) == ("running", None, ME)

    assert kd.reconcile_orphaned_running(live) == []
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"
    assert not [e for e in kb.list_events(live, tid) if e.kind == "reconciled"]
    gate.__exit__(None, None, None)  # the unused lease is released, as the refused claim does


def _age_out_mirror_only_claim(live, tid):
    """Past the grace with the lease lapsed: the claim that the grant began never landed locally."""
    live.execute("UPDATE tasks SET lease_expires = ? WHERE id = ?", (int(time.time() - 5) * 1000, tid))
    live.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ? AND kind = 'claimed'",
                 (kd.ORPHAN_CLAIM_GRACE_SECONDS + 60, tid))
    live.commit()


@pytest.mark.parametrize("prior_impl_run", [False, True], ids=["fresh-review-card", "after-implementation-run"])
def test_board_grant_row_for_review_card_requeues_to_review_after_grace(live, kanban_home, svc, prior_impl_run):
    """Follow-up to the grant-row test (t_01a182bbad9df5ac): once the grace passed and the lease lapsed, the
    mirror-only ``running`` row of a REVIEW card is a real orphan. No local claim opened a run, so the current
    run (if any) is not the reviewer's; the card must still go back to ``review``, not to ``ready``."""
    tid = kb.create_task(live, title="review mirror-only", assignee="worker")
    if prior_impl_run:
        _round(kanban_home, svc)
        assert kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
        assert kb.request_review(live, tid, summary="impl done")
    else:
        live.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        live.commit()
    assert kb.get_task(live, tid).status == "review"
    _round(kanban_home, svc)
    gate = sync.claim_gate(live, tid, int(time.time()) + 900, lane="review")
    assert gate.lease is not None and gate.lease.granted
    _round(kanban_home, svc)  # the grant's row reaches us: running, our lease, no local claim
    row = live.execute("SELECT status, claim_lock, lease_holder FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["status"], row["claim_lock"], row["lease_holder"]) == ("running", None, ME)

    _age_out_mirror_only_claim(live, tid)
    assert kd.reconcile_orphaned_running(live) == [tid]
    assert kb.get_task(live, tid).status == "review"
    (ev,) = [e for e in kb.list_events(live, tid) if e.kind == "reconciled"]
    assert ev.payload["retry_status"] == "review"
    gate.__exit__(None, None, None)


def test_a_run_that_ends_before_its_first_push_survives_the_grants_own_run_row(live, kanban_home, svc):
    """The Board's grant wrote the run row before the local claim did; that copy reaching the gateway after the
    run already ended (a slow round) must not reopen it."""
    tid, uid = _claimed(live, kanban_home, svc, "slow round")
    live.execute("UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?", (int(time.time()) - 5, tid))
    assert kb.release_stale_claims(live) == 0
    assert _run(live, uid)["status"] == "running"  # no competing local finalization
    _expire_board_lease(svc, tid, uid)
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "reclaimed")


def test_a_grant_whose_local_claim_lost_ends_its_run_everywhere(live, kanban_home, svc, monkeypatch):
    tid = kb.create_task(live, title="cas lost", assignee="worker")
    _round(kanban_home, svc)
    monkeypatch.setattr(kb, "claim_and_open_run", lambda *a, **k: None)  # another claimer won the local CAS
    assert kb.claim_task(live, tid, ttl_seconds=900) is None
    assert not svc.board(BOARD)["leases"]  # the unused lease was handed back
    (uid,) = svc.board(BOARD)["rows"]["task_runs"]
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    _assert_ended(live, svc, uid, "released")
    assert kb.get_task(live, tid).status == "ready"


def test_a_newer_board_write_to_a_run_still_lands(live, kanban_home, svc):
    """The fence only drops copies we already have: a later Board write (another writer's end) applies."""
    _tid, uid = _claimed(live, kanban_home, svc, "board ends it")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    b = svc.board(BOARD)
    svc._put(b, "task_runs", uid, {"status": "reclaimed", "outcome": "reclaimed", "ended_at": 7}, "server", None,
             f"{9_999_999_999_999:013d}-0000-server")
    _round(kanban_home, svc)
    assert tuple(_run(live, uid)) == ("reclaimed", "reclaimed", 7)
    assert live.execute("SELECT sync_version FROM task_runs WHERE uid = ?", (uid,)).fetchone()[0] == \
        _board_run(svc, uid)["version"]


def test_the_dispatcher_closes_a_dangling_run_and_the_board_follows(live, kanban_home, svc):
    """Backstop for rows left behind before the fence (and any path not found yet): a run still ``running`` that
    is no longer its card's current run is closed once it is old enough to have settled."""
    tid, uid = _claimed(live, kanban_home, svc, "dangling")
    _round(kanban_home, svc)
    _live_tid, live_uid = _claimed(live, kanban_home, svc, "live run")  # a current run must never be touched
    # What the race left on every host: the card moved on, the run row did not. Made after the last round: a round
    # pushes before it pulls (t_01a1d36ddcea59fe), so the Board's lease release would end the run properly first.
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, "
                     "current_run_id = NULL WHERE id = ?", (tid,))
    young = kb.create_task(live, title="young", assignee="worker")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
                     (young, int(time.time())))
    old = int(time.time()) - kd.DANGLING_RUN_MIN_AGE_SECONDS - 1
    live.execute("UPDATE task_runs SET started_at = ? WHERE uid IN (?, ?)", (old, uid, live_uid))
    closed = kd.close_dangling_runs(live)
    assert [live.execute("SELECT uid FROM task_runs WHERE id = ?", (i,)).fetchone()[0] for i in closed] == [uid]
    assert kd.close_dangling_runs(live) == []  # idempotent
    _round(kanban_home, svc)
    assert _run(live, uid)["status"] == "reclaimed" and _run(live, uid)["ended_at"]
    assert _board_run(svc, uid)["status"] == "reclaimed" and _board_run(svc, uid)["ended_at"]
    assert _run(live, live_uid)["status"] == "running" and _board_run(svc, live_uid)["status"] == "running"
    assert live.execute("SELECT count(*) FROM task_runs WHERE task_id = ? AND status = 'running'",
                        (young,)).fetchone()[0] == 1


@pytest.mark.parametrize("frozen", [False, True], ids=["unsent", "frozen-retry"])
def test_authoritative_completion_replaces_pending_synthetic_dangling_end(live, kanban_home, svc, frozen):
    from hermes_cli import kanban_sync_envelope as envelopes

    tid, uid = _claimed(live, kanban_home, svc, "completion versus stale cleanup")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (tid,))
        live.execute("UPDATE task_runs SET started_at=? WHERE uid=?",
                     (int(time.time()) - kd.DANGLING_RUN_MIN_AGE_SECONDS - 1, uid))
    assert kd.close_dangling_runs(live)
    pending = dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='task_runs' "
                                "AND row_uid=? AND state='pending' ORDER BY rowid DESC LIMIT 1", (uid,)).fetchone())
    saved = None
    if frozen:
        with kbc.write_txn(live):
            envelopes.outbound(live, pending, lambda: ksc.wire_ops.translate(
                ksc._full_row_if_unseen(live, pending), ksc._next_hlc(live, ME)))
        saved = tuple(live.execute("SELECT op_id, hlc, wire_json FROM kanban_sync_outbox WHERE op_id=?",
                                  (pending["op_id"],)).fetchone())
        assert saved[2] is not None
        assert json.loads(saved[2])["fields"]["outcome"] == "reclaimed"
    metadata = json.dumps({"verdict": "approve", "pr_url": "https://github.com/example/repo/pull/573",
                           "head_sha": "6b4d4f1f5f6f16440999a4dcce83dc065eedd1c8"})
    svc._put(svc.board(BOARD), "task_runs", uid,
             {"status": "done", "outcome": "completed", "ended_at": 123, "summary": "review passed",
              "metadata": metadata, "error": None}, "worker-a4", None, "9999999999999-0000-worker-a4")
    version = _board_run(svc, uid)["version"]
    ksc.pull(live, svc.hub(ME), BOARD, observers=False)
    row = live.execute("SELECT status, outcome, ended_at, summary, metadata, error, sync_version "
                       "FROM task_runs WHERE uid=?", (uid,)).fetchone()
    assert tuple(row) == ("done", "completed", 123, "review passed", metadata, None, version)
    if frozen:
        assert saved is not None
        assert tuple(live.execute("SELECT op_id, hlc, wire_json FROM kanban_sync_outbox WHERE op_id=?",
                                  (pending["op_id"],)).fetchone()) == saved
        replay = envelopes.outbound(live, pending, lambda: pytest.fail("must not rebuild frozen cleanup"))
        assert replay == json.loads(saved[2])
    else:
        _round(kanban_home, svc)
        assert _board_run(svc, uid)["outcome"] == "completed"
        assert _board_run(svc, uid)["metadata"] == metadata


def test_dispatch_pass_runs_the_dangling_run_sweep(live, monkeypatch):
    called = []
    monkeypatch.setattr(kd, "close_dangling_runs", lambda conn, **kw: called.append(conn) or [])
    kd.dispatch_once(live, dry_run=True)
    assert called


def test_the_dangling_sweep_keeps_the_real_end_of_a_run_the_card_already_finished(live, kanban_home, svc):
    """A completed run whose row came back ``running`` (sync race / recovery) is restored to completed, not reclaimed."""
    tid, uid = _claimed(live, kanban_home, svc, "done then reopened")
    assert kb.complete_task(live, tid, summary="all done")
    done = live.execute("SELECT id FROM task_runs WHERE uid = ?", (uid,)).fetchone()
    old = int(time.time()) - kd.DANGLING_RUN_MIN_AGE_SECONDS - 1
    with kbc.write_txn(live):  # the reopen: status back to running, end wiped
        live.execute("UPDATE task_runs SET status = 'running', outcome = NULL, ended_at = NULL, started_at = ? "
                     "WHERE id = ?", (old, done["id"]))
    assert kd.close_dangling_runs(live) == [done["id"]]
    row = live.execute("SELECT status, outcome, error, ended_at FROM task_runs WHERE id = ?", (done["id"],)).fetchone()
    assert (row["status"], row["outcome"], row["error"]) == ("done", "completed", None)
    assert row["ended_at"] == live.execute(
        "SELECT created_at FROM task_events WHERE run_id = ? AND kind = 'completed'", (done["id"],)).fetchone()[0]


def _complete_in_capture_gap(live, kanban_home, tid, summary):
    """``complete_task`` landing while another process had the capture triggers down (hub, 10-29 00:33)."""
    with kbc.write_txn(live):
        sync.drop_triggers(live)
    assert kb.complete_task(live, tid, summary=summary, force=True)
    other = kbc.connect(kanban_home / "kanban.db")  # the next process re-installs the triggers (uid-less gap heal)
    other.close()


def test_a_completion_in_a_capture_gap_reaches_the_board_as_completed_not_released(live, kanban_home, svc):
    """Hub t_01a185075c59bd52 (10-29): the uncaptured completion was healed for the tasks row only. The Board took
    ``done`` while the lease's run was still open and ended it ``released`` ("card left running (done)"); that
    row came back over the local ``done/completed`` run, the owner published the card unverified, and the origin
    chat never released it. The run row must reach the Board with the card."""
    tid, uid = _claimed(live, kanban_home, svc, "gap completion")
    _round(kanban_home, svc)
    _round(kanban_home, svc)  # settled: board, replica and Board agree on the task and run versions
    _complete_in_capture_gap(live, kanban_home, tid, "shipped it")
    for _ in range(3):
        _round(kanban_home, svc)
    _assert_completed(live, svc, uid)


def test_a_run_the_boards_placeholder_clobbered_is_restored_from_its_completion(live, kanban_home, svc):
    """Repair of rows already hit: the card is done, its latest ``completed`` event names the run, and the run reads
    the Board's placeholder end. The heal restores ``done/completed`` locally and on the Board."""
    tid, uid = _claimed(live, kanban_home, svc, "clobbered")
    _round(kanban_home, svc)
    assert kb.complete_task(live, tid, summary="real end", force=True)
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    # The state the race left on the hub: placeholder end on the Board and, through the pull, locally.
    b = svc.board(BOARD)
    svc._put(b, "task_runs", uid, {"status": "released", "outcome": "released", "summary": None,
                                   "error": "card left running (done)"}, "server", None,
             f"{9_999_999_999_999:013d}-0000-server")
    _round(kanban_home, svc)  # the pull lands the placeholder; the heal right after it restores the run
    _round(kanban_home, svc)
    _assert_completed(live, svc, uid)
    assert live.execute("SELECT summary FROM task_runs WHERE uid = ?", (uid,)).fetchone()[0] == "real end"


def test_the_completion_heal_leaves_a_run_released_for_another_reason(live, kanban_home, svc):
    """Guard: a placeholder-ended run that is not the one the card's completion names is not a completion."""
    tid, uid = _claimed(live, kanban_home, svc, "not this run")
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("UPDATE task_runs SET status = 'released', outcome = 'released', "
                     "error = 'card left running (done)', ended_at = 5 WHERE uid = ?", (uid,))
        live.execute("UPDATE tasks SET status = 'done', completed_at = 5, current_run_id = NULL WHERE id = ?", (tid,))
    assert divergence.heal_clobbered_completions(live) == []  # no completed event names this run
    assert tuple(_run(live, uid))[:2] == ("released", "released")


def _assert_completed(live, svc, uid):
    local = _run(live, uid)
    assert (local["status"], local["outcome"]) == ("done", "completed"), dict(local)
    board = _board_run(svc, uid)
    assert (board["status"], board["outcome"]) == ("done", "completed"), board
