"""F5: the Board echo of THIS claim's own lease grant must not count against, or lose, the claim it belongs to.

The Board's ``grantLease`` writes the card ``running`` with the new run and ``lease_holder`` = the claimer
(hermes-hub worker/src/transitions.ts; ``FakeService(lease_runs=True)`` mirrors it). ``claim_gate`` acquires that
grant over the network BEFORE ``claim_transaction`` takes the write txn and admission.lock, so the sync pull can
apply the echo in between: the row is then ``running``, ``claim_lock IS NULL``, own holder. Before the fix:

* ``active_card_runs()`` counted that row (own holder), so six real local runs + this claim's own unused grant read
  7/7 and the final gate refused ``hub_capacity_full`` (a foreign worker's lockless echo read 6/7);
* even with room under the cap, the CAS ``WHERE status = <lane> AND claim_lock IS NULL`` lost to the echoed
  ``running`` status, so the claim silently returned None and released its grant.

Every case runs real ``claim_task`` / ``claim_review_task`` with the real lease client (``HubLeaseClient`` over
``FakeService``) and a real sync round (``sync_board``) interleaved at the named point. Only the ~1 s CPU sample is
substituted. Nothing reads or writes live state: the installation root is under ``tmp_path``.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_grant_echo as ge
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.hub_admission_support import locked

pytestmark = pytest.mark.platforms("posix")

ME, BOARD = "hub", "default"
LANES = ["ready", "review"]


def _config(home, cap):
    (home / "config.yaml").write_text(
        f"kanban:\n  max_in_progress: {cap}\n  admission:\n    cpu_busy_below_pct: 50\n"
        "  sync:\n    enabled: true\n")


class Rig:
    def __init__(self, tmp_path, monkeypatch, cap=7):
        self.home = support.kanban_home(tmp_path, monkeypatch)
        _config(self.home, cap)
        self.svc = FakeService(lease_runs=True)
        self.conn = kbc.connect(self.home / "kanban.db")
        ksc.ensure_identity(self.conn, BOARD, ME)
        sync.set_mode(self.conn, "primary")
        self._previous = sync.set_lease_client(ksc.HubLeaseClient(self.svc.hub(ME)))
        self.samples = []
        mm.record_hub_cpu_sample({"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000,
                                  "cpu_busy_sample_end_ms": int(time.time() * 1000)}, root=self.home)
        self.mp = monkeypatch
        locked(monkeypatch, self._sample)

    def _sample(self, *a, **k):
        self.samples.append(time.monotonic())
        return {"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": int(time.time() * 1000)}

    def close(self):
        sync.set_lease_client(self._previous)
        self.conn.close()

    def round(self):
        res = ksc.sync_board(BOARD, self.home / "kanban.db", self.svc.hub(ME), ME)
        assert res.error is None and (res.push is None or res.push.rejected == 0), res
        # The real sampler runs every 5 s; these multi-round echo scenarios can
        # outlive prelease freshness. Refresh its file, not production limits.
        mm.record_hub_cpu_sample({"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000,
                                  "cpu_busy_sample_end_ms": int(time.time() * 1000)}, root=self.home)
        return res

    def card(self, lane="ready", title="echo"):
        t = kb.create_task(self.conn, title=title, assignee="worker")
        if lane == "review":
            with kb.write_txn(self.conn):
                self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (t,))
        self.round()
        return t

    def fill(self, n):
        started = []
        for i in range(n):
            t = self.card(title=f"local{i}")
            got = kb.claim_task(self.conn, t, claimer=f"{kb._host_prefix()}:{i}")
            assert got is not None, sync.pop_claim_deferral(t)
            started.append(t)
        self.round()
        assert mm.active_card_runs() == n
        return started

    def echo_between_gate_and_txn(self, before_txn=None):
        """Wrap the real ``claim_gate``: after the Board grants, run a real sync round (the grant's echo is applied)
        before the claim transaction starts, as the gateway's sync loop can."""
        real = sync.claim_gate
        seen = {}

        def gate_then_pull(conn, task_id, expires, lane="ready"):
            gate = real(conn, task_id, expires, lane)
            if gate.lease is not None:
                seen["gate"] = gate
                self.round()
                if before_txn:
                    before_txn(conn, task_id, gate)
            return gate

        self.mp.setattr(sync, "claim_gate", gate_then_pull)
        return seen

    def row(self, t):
        return self.conn.execute("SELECT status, claim_lock, lease_holder, current_run_id FROM tasks WHERE id=?",
                                 (t,)).fetchone()


def claim(conn, t, lane):
    return (kb.claim_review_task if lane == "review" else kb.claim_task)(conn, t)


def task_runs(conn, t):
    return [dict(r) for r in conn.execute("SELECT id, uid, status, claim_lock FROM task_runs WHERE task_id=?",
                                          (t,))]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    try:
        yield r
    finally:
        r.close()


def test_rig_round_refreshes_expired_advisory_without_relaxing_prelease_limit(rig):
    """Long echo scenarios model a healthy sampler, not a single startup sample."""
    t = rig.card()
    assert ka.PRELEASE_MAX_AGE_MS == 10_000
    mm.record_hub_cpu_sample({"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000,
                              "cpu_busy_sample_end_ms": int(time.time() * 1000) - 11_000}, root=rig.home)
    assert kb.claim_task(rig.conn, t) is None
    assert sync.pop_claim_deferral(t) == "hub_cpu_unknown"
    rig.round()
    assert ka.hub_cpu_advisory(t, conn=rig.conn) is None
    assert kb.claim_task(rig.conn, t)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("kind,regular,priority,pool", [("normal", 6, 3, "regular"), ("urgent", 7, 2, "priority")])
def test_enabled_pools_exclude_only_own_echo_and_preserve_receipt(rig, lane, kind, regular, priority, pool):
    import json

    rig.fill(regular)
    text = (rig.home / "config.yaml").read_text().replace(
        "    cpu_busy_below_pct: 50\n", "    cpu_busy_below_pct: 50\n    priority_slots: 3\n")
    (rig.home / "config.yaml").write_text(text)
    for _ in range(priority):
        marked = rig.card()
        kb.edit_task(rig.conn, marked, admission_class="urgent")
        rig.round()
        assert kb.claim_task(rig.conn, marked), sync.pop_claim_deferral(marked)
    candidate = rig.card(lane)
    kb.edit_task(rig.conn, candidate, admission_class=kind)
    rig.round()
    seen = rig.echo_between_gate_and_txn()
    assert claim(rig.conn, candidate, lane)
    rows = rig.conn.execute("SELECT uid,admission_pool,admission_class,admission_origin FROM task_runs WHERE task_id=?", (candidate,)).fetchall()
    assert len(rows) == 1
    receipt = tuple(rows[0])
    assert receipt[0] == seen["gate"].lease.run_uid
    assert receipt[1:3] == (pool, kind)
    evidence = json.loads(rig.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='claimed' AND json_extract(payload,'$.hub_admission') IS NOT NULL", (candidate,)).fetchone()[0])["hub_admission"]
    assert evidence["excluded_run_uid"] == receipt[0]
    assert evidence["active_after"] == regular + priority
    assert evidence["active_before"] == regular + priority + 1
    rig.round()
    assert tuple(rig.conn.execute("SELECT uid,admission_pool,admission_class,admission_origin FROM task_runs WHERE task_id=?", (candidate,)).fetchone()) == receipt


def test_frozen_counter_shape_own_lockless_echo_counts_foreign_does_not(rig):
    """The counter semantics F5 measured, through the real function on a real board: own-holder lockless counts,
    foreign-holder lockless does not. The generic counter is deliberately unchanged by the fix."""
    rig.fill(6)
    own, foreign = rig.card(title="own"), rig.card(title="foreign")
    with kb.write_txn(rig.conn):
        rig.conn.execute("UPDATE kanban_sync_state SET applying = 1")
        rig.conn.execute("UPDATE tasks SET status='running', lease_holder=? WHERE id=?", (ME, own))
        rig.conn.execute("UPDATE tasks SET status='running', lease_holder='worker-2' WHERE id=?", (foreign,))
        rig.conn.execute("UPDATE kanban_sync_state SET applying = 0")
    assert mm.active_card_runs() == 7  # own lockless still occupies a slot generically (stale/echo fail-closed)
    with kb.write_txn(rig.conn):
        rig.conn.execute("UPDATE kanban_sync_state SET applying = 1")
        rig.conn.execute("UPDATE tasks SET status='ready', lease_holder=NULL WHERE id=?", (own,))
        rig.conn.execute("UPDATE kanban_sync_state SET applying = 0")
    assert mm.active_card_runs() == 6


def test_pool_census_skips_foreign_lockless_holder_like_the_scalar(rig):
    rig.fill(6)
    foreign = rig.card(title="foreign")
    with kb.write_txn(rig.conn):
        rig.conn.execute("UPDATE kanban_sync_state SET applying = 1")
        rig.conn.execute("UPDATE tasks SET status='running', lease_holder='worker-2' WHERE id=?", (foreign,))
        rig.conn.execute("UPDATE kanban_sync_state SET applying = 0")
    detail = {"pools": {"regular": 0, "priority": 0}}
    assert mm.active_card_runs(detail=detail) == 6
    assert detail["pools"] == {"regular": 6, "priority": 0}


@pytest.mark.parametrize("lane", LANES)
def test_six_locals_plus_this_claims_own_grant_echo_admits_exactly_one_run(rig, lane):
    """RED before the fix: refused ``hub_capacity_full`` (the claim's own unused grant read as the 7th run)."""
    rig.fill(6)
    t = rig.card(lane)
    seen = rig.echo_between_gate_and_txn()
    claimed = claim(rig.conn, t, lane)
    assert "gate" in seen  # the echo really interleaved
    assert sync.pop_claim_deferral(t) is None
    assert claimed is not None and claimed.status == "running"
    runs = task_runs(rig.conn, t)
    assert len(runs) == 1 and runs[0]["status"] == "running" and runs[0]["claim_lock"] is not None
    assert runs[0]["uid"] == seen["gate"].lease.run_uid and runs[0]["id"] == claimed.current_run_id
    assert mm.active_card_runs() == 7
    ev = kb._latest_event(rig.conn, t, "claimed", claimed.current_run_id)
    payload = kb._json_dict(ev["payload"])
    assert payload.get("hub_cpu") and (payload.get("source_status") == "review") == (lane == "review")
    assert payload["hub_admission"] == {"active_before": 7, "active_after": 6,
                                        "excluded_run_uid": seen["gate"].lease.run_uid}
    assert rig.svc.board(BOARD)["leases"][t]["run_uid"] == seen["gate"].lease.run_uid  # grant kept, not released
    # The next claim (8th) is refused: the admitted run now holds a real lock and counts.
    t8 = rig.card(lane, title="eighth")
    assert claim(rig.conn, t8, lane) is None
    assert sync.pop_claim_deferral(t8) == "hub_capacity_full"


@pytest.mark.parametrize("lane", LANES)
def test_cas_adopts_this_claims_own_echo_with_room_under_the_cap(tmp_path, monkeypatch, lane):
    """RED before the fix even with the cap out of the way: the echoed ``running`` status made the CAS lose."""
    r = Rig(tmp_path, monkeypatch, cap=50)
    try:
        t = r.card(lane)
        r.echo_between_gate_and_txn()
        claimed = claim(r.conn, t, lane)
        assert claimed is not None
        assert len(task_runs(r.conn, t)) == 1
    finally:
        r.close()


@pytest.mark.parametrize("lane", LANES)
def test_seven_real_locals_still_refuse_even_with_own_echo(rig, lane):
    rig.fill(7)
    t = rig.card(lane)
    rig.echo_between_gate_and_txn()
    assert claim(rig.conn, t, lane) is None
    assert sync.pop_claim_deferral(t) == "hub_capacity_full"
    assert task_runs(rig.conn, t)[0]["claim_lock"] is None  # only the echo placeholder; no local run started
    assert rig.svc.board(BOARD)["leases"].get(t) is None  # the unused grant was released


@pytest.mark.parametrize("lane", LANES)
def test_prior_unrelated_own_echo_is_still_counted(rig, lane):
    """Only THIS claim's grant is excluded: another card's own-holder lockless row (a stale echo) still counts."""
    rig.fill(5)
    other = rig.card(title="other-echo")
    # Another card's grant to this gateway, echoed but never started locally (e.g. an in-flight claim elsewhere).
    state = sync._state(rig.conn)
    assert sync._client().acquire(board_id=state["board_id"], task_uid=other, run_uid=sync.uuid7(),
                                  gateway_id=ME, ttl_s=900).granted
    rig.round()
    assert rig.row(other)["status"] == "running" and rig.row(other)["claim_lock"] is None
    assert mm.active_card_runs() == 6
    t = rig.card(lane)
    rig.echo_between_gate_and_txn()
    # 5 locals + other echo + own echo = 7; excluding only own leaves 6 -> admitted.
    assert claim(rig.conn, t, lane) is not None
    assert mm.active_card_runs() == 7
    t2 = rig.card(lane, title="next")
    rig.echo_between_gate_and_txn()
    assert claim(rig.conn, t2, lane) is None  # 5 + other + t + own echo(excluded) = 7
    assert sync.pop_claim_deferral(t2) == "hub_capacity_full"


def _mutate(conn, sql, args=()):
    with kb.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute(sql, args)
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


MISMATCHES = {
    # The echo's holder is not this gateway (a foreign worker's grant is not ours to exclude; it isn't counted).
    "foreign_holder": ("UPDATE tasks SET lease_holder='worker-2' WHERE id=?", "foreign"),
    "null_holder": ("UPDATE tasks SET lease_holder=NULL WHERE id=?", "counted"),
    "expired_lease": ("UPDATE tasks SET lease_expires=1000 WHERE id=?", "counted"),
    "missing_lease_expiry": ("UPDATE tasks SET lease_expires=NULL WHERE id=?", "counted"),
    "run_uid_mismatch": ("UPDATE task_runs SET uid='other-run' WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                         "counted"),
    "no_current_run": ("UPDATE tasks SET current_run_id=NULL WHERE id=?", "counted"),
    "locked_row": ("UPDATE tasks SET claim_lock='someone:1' WHERE id=?", "counted"),
    "locked_run": ("UPDATE task_runs SET claim_lock='someone:1' WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                   "counted"),
    "ended_run": ("UPDATE task_runs SET ended_at=5 WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", "counted"),
    "terminal_run": ("UPDATE task_runs SET status='released' WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                     "counted"),
    "run_of_other_task": ("UPDATE task_runs SET task_id='t_other' WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                          "counted"),
    "not_running": ("UPDATE tasks SET status='blocked' WHERE id=?", "not_counted"),
}


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("case", sorted(MISMATCHES))
def test_any_identity_mismatch_fails_closed(rig, lane, case):
    """Exclusion needs EVERY predicate: own holder, live lease, the granted run uid as the current run, no lock."""
    sql, kind = MISMATCHES[case]
    rig.fill(6)
    t = rig.card(lane)
    rig.echo_between_gate_and_txn(lambda conn, tid, gate: _mutate(conn, sql, (tid,)))
    assert claim(rig.conn, t, lane) is None
    reason = sync.pop_claim_deferral(t)
    if kind in ("foreign", "not_counted"):
        # Not counted (6/7), so the cap is not the refusal; the CAS still refuses a row it cannot prove is ours.
        assert reason is None
    else:
        assert reason == "hub_capacity_full"
    assert rig.row(t)["claim_lock"] in (None, "someone:1") and len(task_runs(rig.conn, t)) <= 1  # nothing started
    assert rig.svc.board(BOARD)["leases"].get(t) is None


@pytest.mark.parametrize("lane", LANES)
def test_consumed_gate_excludes_nothing(rig, lane, monkeypatch):
    rig.fill(6)
    t = rig.card(lane)

    def consume(conn, tid, gate):
        gate.consumed = True
    rig.echo_between_gate_and_txn(consume)
    assert claim(rig.conn, t, lane) is None
    assert sync.pop_claim_deferral(t) == "hub_capacity_full"


def test_exclusion_is_bound_to_the_claiming_board_identity(rig, tmp_path, monkeypatch):
    """A matching row on ANOTHER board file (same task id, same holder, same run uid) is never excluded: the
    exclusion names the claim connection's (st_dev, st_ino)."""
    rig.fill(6)
    t = rig.card()
    seen = rig.echo_between_gate_and_txn()
    real = mm.active_card_runs
    calls = []

    def wrong_board(exclude=None, detail=None):
        calls.append(exclude)
        return real(exclude=None if exclude is None else exclude._replace(db_identity=(0, 0)), detail=detail)
    monkeypatch.setattr(mm, "active_card_runs", wrong_board)
    assert claim(rig.conn, t, "ready") is None
    assert sync.pop_claim_deferral(t) == "hub_capacity_full"
    assert calls and calls[0] is not None and calls[0].run_uid == seen["gate"].lease.run_uid


def test_replayed_grant_echo_after_release_is_not_adopted(rig):
    """A refused claim releases its grant; the Board's release echo makes the card ready again. A later claim
    gets a NEW grant/run uid: the old run row is never adopted and exactly one run row is live."""
    rig.fill(7)
    t = rig.card()
    rig.echo_between_gate_and_txn()
    assert claim(rig.conn, t, "ready") is None
    rig.round()  # release echo
    assert rig.row(t)["status"] == "ready"
    done = kb.list_tasks(rig.conn, status="running")[0].id
    assert kb.complete_task(rig.conn, done, result="ok")
    rig.round()
    seen = rig.echo_between_gate_and_txn()
    claimed = kb.claim_task(rig.conn, t)
    assert claimed is not None
    live = [r for r in task_runs(rig.conn, t) if r["status"] == "running"]
    assert len(live) == 1 and live[0]["uid"] == seen["gate"].lease.run_uid


def test_echo_after_the_transaction_keeps_one_run(rig):
    """Echo-after-transaction schedule: no echo before the CAS (normal path), the echo then lands on the claimed row
    and must neither fork the run nor change the count."""
    rig.fill(6)
    t = rig.card()
    claimed = kb.claim_task(rig.conn, t)
    assert claimed is not None
    rig.round()
    assert len(task_runs(rig.conn, t)) == 1
    assert mm.active_card_runs() == 7


def test_two_simultaneous_claimers_at_six_admit_at_most_one(rig, tmp_path):
    """Both grants echoed before either transaction; two real claimers on separate connections race the gate."""
    rig.fill(6)
    a, b = rig.card(title="a"), rig.card(title="b")
    gates = {}
    barrier = threading.Barrier(2)
    real = sync.claim_gate
    pulled = threading.Event()
    lock = threading.Lock()

    def gate_then_wait(conn, task_id, expires, lane="ready"):
        gate = real(conn, task_id, expires, lane)
        gates[task_id] = gate
        barrier.wait(timeout=10)
        with lock:
            if not pulled.is_set():
                rig.round()  # one pull applies both grants' echoes
                pulled.set()
        return gate
    rig.mp.setattr(sync, "claim_gate", gate_then_wait)
    results = {}

    def go(t):
        c = kbc.connect(rig.home / "kanban.db")
        try:
            results[t] = kb.claim_task(c, t)
        finally:
            c.close()
    threads = [threading.Thread(target=go, args=(t,)) for t in (a, b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    admitted = [t for t, v in results.items() if v is not None]
    assert len(admitted) <= 1
    rig.round()  # the refused claimer's release echo
    locked = rig.conn.execute("SELECT count(*) FROM tasks WHERE status='running' AND claim_lock IS NOT NULL"
                              ).fetchone()[0]
    assert locked <= 7


def test_unreadable_other_board_still_refuses(rig, monkeypatch):
    rig.fill(6)
    t = rig.card()
    rig.echo_between_gate_and_txn()
    from hermes_cli import kanban_db_boards
    bad = rig.home / "kanban" / "boards" / "broken" / "kanban.db"
    monkeypatch.setattr(kanban_db_boards, "named_board_db_files", lambda: [("broken", bad)])
    assert claim(rig.conn, t, "ready") is None
    assert sync.pop_claim_deferral(t) == "hub_capacity_full"


def test_db_identity_of_a_connection_matches_the_counter_identity(rig):
    import os
    st = os.stat(rig.home / "kanban.db")
    assert ge.connection_db_identity(rig.conn) == (st.st_dev, st.st_ino)
    mem = sqlite3.connect(":memory:")
    try:
        assert ge.connection_db_identity(mem) is None
    finally:
        mem.close()


class _Gate:
    """A copy of a real gate's identity with one field changed (for the predicate-by-predicate proofs below)."""

    def __init__(self, real, **over):
        self.lease = over.get("lease", real.lease)
        self._ids = over.get("ids", real._ids)
        self.consumed = over.get("consumed", False)


def test_own_grant_echo_requires_every_identity_predicate(rig, tmp_path):
    """With the echo really applied (inside the claim's own txn), the real gate proves it; each changed field fails
    closed. Then the counter's own re-check: identity/gateway mismatch excludes nothing, a moved row is unknown."""
    rig.fill(6)
    t = rig.card()
    got = {}

    def probe(conn, tid, gate):
        echo = ge.own_grant_echo(conn, tid, gate)
        got["echo"] = echo
        lease = gate.lease
        variants = {
            "consumed": _Gate(gate, consumed=True),
            "no_lease": _Gate(gate, lease=None),
            "denied": _Gate(gate, lease=lease.__class__(granted=False, run_uid=lease.run_uid)),
            "empty_run_uid": _Gate(gate, lease=lease.__class__(granted=True, run_uid="", expires_ms=lease.expires_ms)),
            "other_run_uid": _Gate(gate, lease=lease.__class__(granted=True, run_uid="x", expires_ms=lease.expires_ms)),
            "other_gateway": _Gate(gate, ids=(gate._ids[0], "worker-2", gate._ids[2])),
            "other_task": _Gate(gate, ids=(gate._ids[0], gate._ids[1], "t_other")),
            "no_ids": _Gate(gate, ids=None),
        }
        got["variants"] = {k: ge.own_grant_echo(conn, tid, g) for k, g in variants.items()}
        got["other_task_id"] = ge.own_grant_echo(conn, "t_other", gate)
        mem = sqlite3.connect(":memory:")
        try:
            got["identity_none"] = ge.connection_db_identity(mem)
        finally:
            mem.close()
        # The counter, re-proving on its own read connection.
        got["count_plain"] = mm.active_card_runs()
        detail = {}
        got["count_excl"] = mm.active_card_runs(exclude=echo, detail=detail)
        got["detail"] = detail
        got["count_other_board"] = mm.active_card_runs(exclude=echo._replace(db_identity=(0, 0)))
        got["count_other_gw"] = mm.active_card_runs(exclude=echo._replace(gateway_id="worker-2"))
        got["count_moved"] = mm.active_card_runs(exclude=echo._replace(run_id=echo.run_id + 999))
        real_identity = ge.connection_db_identity
        ge.connection_db_identity = lambda c: None
        try:
            got["identity_unknown"] = ge.own_grant_echo(conn, tid, gate)
        finally:
            ge.connection_db_identity = real_identity
        sync.set_mode(conn, "off")
        got["off_mode"] = ge.own_grant_echo(conn, tid, gate)
        sync.set_mode(conn, "primary")
        raise RuntimeError("stop before the claim txn")

    rig.echo_between_gate_and_txn(probe)
    with pytest.raises(RuntimeError):
        kb.claim_task(rig.conn, t)
    assert got["echo"] is not None and got["echo"].task_id == t and got["echo"].gateway_id == ME
    assert got["variants"] == {k: None for k in got["variants"]}
    assert got["other_task_id"] is None and got["identity_none"] is None and got["off_mode"] is None
    assert got["identity_unknown"] is None
    assert (got["count_plain"], got["count_excl"], got["detail"]) == (7, 6, {"excluded": 1})
    assert got["count_other_board"] == 7 and got["count_other_gw"] == 7
    assert got["count_moved"] is None


@pytest.mark.parametrize("lane", LANES)
def test_cas_rechecks_the_echo_in_the_same_txn(rig, lane):
    """The echo bound before the gate is re-proven right before the CAS: a change to the row inside the txn (here
    by the caller's ``admission_check``) makes the claim refuse rather than adopt a row it no longer owns."""
    t = rig.card(lane)
    rig.echo_between_gate_and_txn()

    def steal():
        rig.conn.execute("UPDATE tasks SET lease_holder='worker-2' WHERE id=?", (t,))
        return False
    fn = kb.claim_review_task if lane == "review" else kb.claim_task
    assert fn(rig.conn, t, admission_check=steal) is None
    assert all(r["claim_lock"] is None for r in task_runs(rig.conn, t))


def test_cas_predicate_itself_names_the_echo_run(rig, monkeypatch):
    """Belt and braces: even if the re-proof were wrong, the CAS only adopts a running row whose current run is
    the echo's run id."""
    t = rig.card()
    rig.echo_between_gate_and_txn()
    real_bind = ka._bind_echo

    def bind_then_move(conn, task_id, gate):
        real_bind(conn, task_id, gate)
        if gate is not None and gate.echo is not None:
            conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id=?", (task_id,))
            monkeypatch.setattr(ge, "match_rows", lambda c, e: [e.run_id])
    monkeypatch.setattr(ka, "_bind_echo", bind_then_move)
    assert kb.claim_task(rig.conn, t) is None


@pytest.mark.parametrize("lane", LANES)
def test_unsupported_fence_platform_still_adopts_the_echo(rig, monkeypatch, lane):
    """Without admission.lock support the claim txn has no CPU/cap gate, but the CAS must still adopt the echo."""
    from hermes_cli import admission_fence as af
    monkeypatch.setattr(af, "SUPPORTED", False)
    t = rig.card(lane)
    rig.echo_between_gate_and_txn()
    claimed = claim(rig.conn, t, lane)
    assert claimed is not None and len(task_runs(rig.conn, t)) == 1


def test_duplicated_board_inode_is_counted_and_excluded_once(rig, monkeypatch):
    """A named-board entry that is the same file (symlink, same st_dev/st_ino) is skipped by the counter, so the own
    echo is excluded exactly once: 6 locals + own echo admits, the 8th claim is refused."""
    from hermes_cli import kanban_db_boards
    alias = rig.home / "alias-kanban.db"
    alias.symlink_to(rig.home / "kanban.db")
    monkeypatch.setattr(kanban_db_boards, "named_board_db_files", lambda: [("alias", alias)])
    rig.fill(6)
    t = rig.card()
    rig.echo_between_gate_and_txn()
    assert kb.claim_task(rig.conn, t) is not None
    assert mm.active_card_runs() == 7
    t8 = rig.card(title="eighth")
    assert kb.claim_task(rig.conn, t8) is None
    assert sync.pop_claim_deferral(t8) == "hub_capacity_full"
