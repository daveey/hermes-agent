"""G2: dispatcher reclaim sub-sweeps run when their input revision moved or a due time from their data arrived.

Design t_01a1d563324d7e8e §3 (G2), review R13, condition C12. A quiet board (no content writes) must still:
reclaim an expired claim within one tick (claim expiry is a bookkeeping column: it never moves the revision),
detect a SIGKILLed local worker within one tick, release a recovered quota block, and run every gated sweep once
its own due time passes with nothing else changing.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_quota as kbq
from hermes_cli import kanban_sweep_gate as gate
from hermes_cli import kanban_wake_check as kwc
from hermes_cli import loop_accounting as la


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    gate.reset()
    yield home
    gate.reset()


def _revision(conn) -> int:
    return conn.execute("SELECT revision FROM kanban_activity_revision").fetchone()[0]


def _tick(conn):
    return kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)


def _runs(name: str) -> tuple[int, int]:
    st = la.counters().get(name, {})
    return st.get("runs", 0), st.get("skips", 0)


# -- the gate itself --------------------------------------------------------------------------------------------

def test_gate_skips_unchanged_input_and_reruns_when_content_moves(home):
    calls = []
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nobody")
        for _ in range(3):
            gate.gated("test.sweep", conn, lambda: calls.append(1))
        assert len(calls) == 1
        kb.add_comment(conn, tid, "someone", "a content write")
        gate.gated("test.sweep", conn, lambda: calls.append(1))
        assert len(calls) == 2


def test_gate_bookkeeping_write_moves_nothing_so_a_due_time_must_carry_it(home):
    """Why every gated sweep needs a data-derived due time: claim/lease columns never bump (R13)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="nobody")
        before = _revision(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = 1, last_heartbeat_at = 2 WHERE id = ?", (tid,))
        assert _revision(conn) == before


def test_gate_runs_when_the_live_due_time_arrives(home):
    calls = []
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="nobody")
        due = lambda c: 1_000  # e.g. MIN(next_at) of a bookkeeping table, read every tick
        gate.gated("test.due", conn, lambda: calls.append(1), due=due, now=900)
        gate.gated("test.due", conn, lambda: calls.append(1), due=due, now=999)
        assert len(calls) == 1
        gate.gated("test.due", conn, lambda: calls.append(1), due=due, now=1_000)
        assert len(calls) == 2


def test_gate_unreadable_due_time_reruns_a_completed_key(home):
    calls = []
    due_reads = []

    def unreadable_due(conn):
        due_reads.append(1)
        # A real SQLite read failure, without changing the board or its key.
        return conn.execute("SELECT next_at FROM missing_due_table").fetchone()[0]

    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="nobody")
        sweep = lambda: calls.append(1)
        gate.gated("test.unreadable_due", conn, sweep, now=900)
        gate.gated("test.unreadable_due", conn, sweep, now=900)
        assert len(calls) == 1  # prove a completed key really suppresses work
        gate.gated("test.unreadable_due", conn, sweep, due=unreadable_due, now=900)
        assert due_reads == [1]
        assert len(calls) == 2, "an unreadable due time must rerun even a completed key"


def test_gate_keeps_the_runs_own_due_time_with_its_key(home):
    calls = []
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="nobody")

        def sweep():
            calls.append(1)
            return 2_000

        for now in (1_000, 1_500, 1_999):
            gate.gated("test.run_due", conn, sweep, run_due=lambda at: at, now=now)
        assert len(calls) == 1
        gate.gated("test.run_due", conn, sweep, run_due=lambda at: at, now=2_000)
        assert len(calls) == 2


def test_gate_without_the_revision_counter_always_runs(home):
    calls = []
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="nobody")
        with kb.write_txn(conn):
            rev.drop_triggers(conn)
        rev._COVERED.clear()
        for _ in range(3):
            gate.gated("test.unknown", conn, lambda: calls.append(1))
        assert len(calls) == 3


def test_gate_failed_run_does_not_vouch(home):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("x")

    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="nobody")
        for _ in range(2):
            with pytest.raises(RuntimeError):
                gate.gated("test.fail", conn, boom)
        assert len(calls) == 2


def test_gate_failed_due_rerun_invalidates_prior_completion(home):
    calls = []

    def sweep():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("transient sweep failure")

    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="t", assignee="nobody")
        gate.gated("test.warm_fail", conn, sweep, now=900)
        gate.gated("test.warm_fail", conn, sweep, now=900)
        assert len(calls) == 1  # establish a reusable prior completion
        with pytest.raises(RuntimeError, match="^transient sweep failure$"):
            gate.gated("test.warm_fail", conn, sweep, due=lambda c: 900, now=900)
        assert len(calls) == 2
        # The transient due signal cleared, but the failed pass still needs retrying.
        gate.gated("test.warm_fail", conn, sweep, now=900)
        assert len(calls) == 3, "a failed rerun must invalidate the prior completed key"
        gate.gated("test.warm_fail", conn, sweep, now=900)
        assert len(calls) == 3  # a successful retry can vouch for the key again


def test_foreign_ids_memo_follows_new_copies(home):
    with kbc.connect_closing() as conn:
        if "created_on" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
            with kb.write_txn(conn):  # a board that never synced has no provenance column yet
                conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
            rev.ensure_revision(conn, rev.LOCAL_TABLES)
        mine = kb.create_task(conn, title="mine", assignee="nobody")
        other = kb.create_task(conn, title="copy", assignee="nobody")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on = 'gw-a' WHERE id = ?", (mine,))
        assert kwc.foreign_ids(conn, "gw-a") == set()
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on = 'gw-b' WHERE id = ?", (other,))
        assert kwc.foreign_ids(conn, "gw-a") == {other}
        assert kwc.foreign_ids(conn, "gw-a") == {other}
        conn.execute("BEGIN IMMEDIATE")  # inside a transaction: the caller's own uncommitted write counts
        conn.execute("UPDATE tasks SET created_on = 'gw-c' WHERE id = ?", (mine,))
        assert kwc.foreign_ids(conn, "gw-a") == {other, mine}
        conn.rollback()
        assert kwc.foreign_ids(conn, "gw-a") == {other}


@pytest.mark.parametrize("sql, index", [
    ("SELECT id FROM task_runs WHERE ended_at IS NOT NULL AND ended_at <= 5 "
     "AND worker_pid IS NOT NULL AND worker_started_at IS NOT NULL", "idx_runs_worker_pid"),
    ("SELECT id FROM tasks WHERE status IN ('ready','todo','review','blocked','scheduled','done') "
     "AND claim_lock IS NOT NULL AND substr(claim_lock, 1, 3) = 'h:'", "idx_tasks_claim_lock"),
    ("SELECT COUNT(*) FROM task_runs WHERE outcome = 'rate_limited' AND ended_at > 5", "idx_runs_rate_limited"),
    ("SELECT id FROM task_runs WHERE ended_at IS NOT NULL AND ended_at >= 5 AND metadata LIKE '%\"blocker_id\"%' "
     "ORDER BY id", "idx_runs_blocker_assert"),
])
def test_liveness_sweeps_start_from_a_partial_index(home, sql, index):
    """Process-liveness sweeps run every tick, so their empty case must not scan every task or run."""
    with kbc.connect_closing() as conn:
        plan = " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert index in plan, plan


# -- the tick: quiet-board guarantees (R13, C12) ---------------------------------------------------------------

@pytest.mark.parametrize("outcome, metadata, want", [
    ("blocked", '{"blocker_id": "Wall X"}', ("assert", "wall-x")),
    ("blocked", '{"clear_blocker": true}', ("clear", None)),
    ("blocked", '{"clear_blocker": false}', ("pass", None)),
    ("blocked", '{"note": "blocker_id mentioned in prose"}', ("pass", None)),
    ("blocked", '{"x": 1}', ("pass", None)),
    ("completed", '{"blocker_id": "w"}', ("clear", None)),
    ("blocked", {"blocker_id": "w"}, ("pass", None)),  # unchanged: _json_dict only decodes text
    ("blocked", None, ("pass", None)),
])
def test_run_effect_fast_path_matches_the_parsed_answer(outcome, metadata, want):
    from hermes_cli.kanban_shared_blockers import run_effect
    assert run_effect(outcome, metadata) == want


def test_expired_claim_on_a_write_free_board_is_reclaimed_within_one_tick(home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="claimed", assignee="nobody")
        assert kb.claim_task(conn, tid)
        _tick(conn)
        _tick(conn)  # every gate now holds its key
        assert kb.get_task(conn, tid).status == "running"
        before = _revision(conn)
        with kb.write_txn(conn):  # the lease runs out: a bookkeeping write, the revision does not move
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 1, tid))
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE task_id = ?", (int(time.time()) - 1, tid))
        assert _revision(conn) == before
        res = _tick(conn)
        assert res.reclaimed >= 1
        assert kb.get_task(conn, tid).status == "ready"


def test_sigkilled_worker_on_a_quiet_board_is_detected_within_one_tick(home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="worker", assignee="nobody")
            assert kb.claim_task(conn, tid)
            kbd._set_worker_pid(conn, tid, proc.pid)
            _tick(conn)
            _tick(conn)
            assert kb.get_task(conn, tid).status == "running"
            before = _revision(conn)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            assert _revision(conn) == before  # process death writes nothing
            res = _tick(conn)
            assert tid in res.crashed
            assert kb.get_task(conn, tid).status != "running"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_quota_recovery_still_fires_after_quiet_ticks(home, monkeypatch):
    err = "pid 82594 exited rate-limited (quota wall) — requeued without counting a failure"
    reason = ("Held in ready by the respawn guard (blocker_auth) for 30 min without running. "
              f"Last error: {err} Fix the cause (credentials, quota, PR binding), then unblock.")
    now = int(time.time()) - 600
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(kb.time, "time", lambda: now)
        tid = kb.create_task(conn, title="held", assignee="nobody-runs-this")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (err, tid))
        assert kb.block_task(conn, tid, reason=reason, kind="capability")
        monkeypatch.undo()
        for _ in range(2):
            assert tid not in (_tick(conn).quota_block_released or [])
        monkeypatch.setattr(kb.time, "time", lambda: now + 60)
        other = kb.create_task(conn, title="other", assignee="b")
        kb.claim_task(conn, other)
        assert kb.complete_task(conn, other, result="ok")
        monkeypatch.undo()
        assert tid in _tick(conn).quota_block_released
        assert kbq  # keeps its own input (blocked rows + completed runs) and cadence: never gated


# -- each gated sweep runs when its own due time passes with no other change ----------------------------------

def test_continuation_park_wakes_at_its_wake_time_with_no_other_change(home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="parked", assignee="nobody")
        wake_at = int(time.time()) + 900
        assert kb.schedule_task(conn, tid, reason=f"SCHEDULED_UNTIL={wake_at} rate-limit cooldown", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"})
        wake_at = kc.pending_wake(conn, tid)["wake_at"]
        _, skips0 = _runs("dispatch.continuation_parks")
        for now in (wake_at - 100, wake_at - 50, wake_at - 1):
            assert kc.wake_due_continuations(conn, now=now) == []
        _, skips1 = _runs("dispatch.continuation_parks")
        assert skips1 - skips0 >= 2, "an unchanged board must not rescan its parks"
        assert kc.wake_due_continuations(conn, now=wake_at) == [tid]
        assert kb.get_task(conn, tid).status in ("ready", "todo")


def test_stranded_blocker_probe_runs_when_its_interval_ends_with_no_other_change(home, monkeypatch):
    from hermes_cli import kanban_shared_blockers as ksb
    from hermes_cli.kanban_idle_candidates import sweep_shared_blockers

    monkeypatch.setattr(ksb, "probe_interval_seconds", lambda cfg=None: 1800)
    t0 = int(time.time())
    with kbc.connect_closing() as conn:
        from unittest import mock

        from hermes_cli import kanban_timed_park as ktp
        tid = kb.create_task(conn, title="behind", assignee="default")
        assert kb.claim_task(conn, tid)
        with mock.patch.object(ktp, "park_mode", lambda *a, **k: None):  # a plain ``blocked`` card, no wake
            assert kb.block_task(conn, tid, reason="wall", kind="capability", blocker_id="wall-x")
        assert kb.get_task(conn, tid).status == "blocked"
        assert ksb.blocker_states(conn)["wall-x"].is_open

        class R:
            shared_blocker_released = shared_blocker_probed = None

        clock = {"t": t0 + 10}
        monkeypatch.setattr(kb.time, "time", lambda: clock["t"])
        r = R()
        sweep_shared_blockers(conn, r)
        assert r.shared_blocker_probed == []
        _, skips0 = _runs("dispatch.shared_blocker_probed")
        clock["t"] = t0 + 1000
        sweep_shared_blockers(conn, r)
        assert _runs("dispatch.shared_blocker_probed")[1] == skips0 + 1  # gated: nothing moved, not yet due
        clock["t"] = t0 + 1801
        sweep_shared_blockers(conn, r)
        assert r.shared_blocker_probed == [tid]
        assert kb.get_task(conn, tid).status != "blocked"


def test_scoped_blocker_probe_reruns_when_a_route_enters_its_scope_with_no_board_write(home, monkeypatch):
    """G2 follow-up (review 5898596145): ``task_applies`` reads the assignee's config, outside the gate key. A
    blocked card whose route moves into a provider-scoped blocker's scope with no board write is probed within
    ``ROUTE_RECHECK_SECONDS``, not at the next content write or the 14-day lookback exit."""
    from unittest import mock

    from hermes_cli import kanban_model_assignment as assignment
    from hermes_cli import kanban_model_tier as tier
    from hermes_cli import kanban_shared_blockers as ksb
    from hermes_cli import kanban_timed_park as ktp
    from hermes_cli.kanban_idle_candidates import sweep_shared_blockers

    # Neutral admission leaves a next route provider-unknown (class/auto), so the externally resolved route is an
    # isolated seam here: a provider the next admission is known to use, readable outside the board's gate key.
    external = {"provider": "openai-codex"}
    monkeypatch.setattr(ksb, "task_route", lambda _conn, _tid: ("route-model", external["provider"], "ready"))
    monkeypatch.setattr(ksb, "probe_interval_seconds", lambda cfg=None: 1800)
    t0 = int(time.time())
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="behind", assignee="default")
        assert kb.claim_task(conn, tid)
        with mock.patch.object(ktp, "park_mode", lambda *a, **k: None):  # a plain ``blocked`` card, no wake
            assert kb.block_task(conn, tid, reason="wall", kind="capability", blocker_id="wall-a",
                                 blocker_provider="anthropic")
        st = ksb.blocker_states(conn)["wall-a"]
        assert st.is_open and st.blocker_provider == "anthropic"
        assert not ksb.task_applies(conn, st, tid)  # routed to openai-codex: outside the scope

        class R:
            shared_blocker_released = shared_blocker_probed = None

        clock = {"t": t0 + 1801}  # the probe interval is over: only the scope keeps the card unprobed
        monkeypatch.setattr(kb.time, "time", lambda: clock["t"])
        r = R()
        sweep_shared_blockers(conn, r)
        assert r.shared_blocker_probed == []
        revision = _revision(conn)
        _, skips0 = _runs("dispatch.shared_blocker_probed")
        clock["t"] += 15  # one more tick, nothing moved: still gated (the cadence keeps the budget)
        sweep_shared_blockers(conn, r)
        assert _runs("dispatch.shared_blocker_probed")[1] == skips0 + 1

        external["provider"] = "anthropic"  # no board write
        assert ksb.task_applies(conn, st, tid)
        assert _revision(conn) == revision
        clock["t"] = t0 + 1801 + 300  # the stated cadence (ksb.ROUTE_RECHECK_SECONDS)
        sweep_shared_blockers(conn, r)
        assert r.shared_blocker_probed == [tid]
        assert kb.get_task(conn, tid).status != "blocked"


def test_unscoped_blocker_adds_no_route_recheck(home, monkeypatch):
    """The cadence is only for provider-scoped blockers: an unscoped one holds every card whatever its route."""
    from unittest import mock

    from hermes_cli import kanban_shared_blockers as ksb
    from hermes_cli import kanban_timed_park as ktp

    t0 = int(time.time())
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="behind", assignee="default")
        assert kb.claim_task(conn, tid)
        with mock.patch.object(ktp, "park_mode", lambda *a, **k: None):
            assert kb.block_task(conn, tid, reason="wall", kind="capability", blocker_id="wall-x")
        due: list = []
        assert ksb.probe_stranded_blockers(conn, now=t0 + 10, interval=1800, due_out=due) == []
        assert due and min(due) > t0 + 10 + 300  # the probe interval's end, not a route recheck
