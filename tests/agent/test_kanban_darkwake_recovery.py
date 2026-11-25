"""A Kanban worker survives a long sleep with networkless DarkWakes (runs 10712 / 10714).

The 10-30 incident: a MacBook slept 16:57-21:31 with several maintenance DarkWakes that had no DNS.
Two managed workers exited 75 during those wakes and the dispatcher then booked both runs
``lease_lost`` (a failure) and gave the cards away:

* 10714 (Anthropic): every stale-stream kill counted toward the cross-turn breaker, although each
  kill only measured the sleep (the stream watchdog runs on wall time), so the fifth wake gave up.
* 10712 (Codex): the turn failed while offline; in-place recovery found the claim lease expired
  (the dispatcher that renews it was asleep too) and refused at once, 14 s before the dispatcher's
  next tick would have extended it.
* Both: the dispatcher's stale sweep ran before its crash sweep, so the worker's own transient exit
  (75 = requeue without a failure) was overwritten by a ``lease_lost`` failure.

These tests drive the real pieces: the stale breaker, the managed owner seam
(``gateway.session_kanban.run_worker_turns``) on a real board, and the real dispatcher sweeps
(``release_stale_claims`` with the suspend ledger, ``detect_crashed_workers``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

# ── Anthropic path: the cross-turn stale breaker ─────────────────────


def test_suspend_caused_stale_kills_do_not_trip_the_breaker(monkeypatch):
    """A stale kill measured across a host suspend is the sleep's, not the provider's: eight
    DarkWake kills in a row leave the streak at 0 and the next call is still allowed."""
    import agent.chat_completion_helpers as h
    import agent.chat_completion_stale_breaker as sb

    monkeypatch.delenv("HERMES_STREAM_STALE_GIVEUP", raising=False)  # default 5
    skew = {"s": 1_000.0}  # wall time minus awake time; grows by the suspended seconds
    monkeypatch.setattr(sb, "_wall_minus_awake", lambda: skew["s"], raising=False)
    agent = SimpleNamespace()
    h._reset_stale_streak(agent)  # the last call before the lid closed completed

    for gap in (3_600, 1_200, 900, 2_400, 600, 1_800, 300, 120):  # DarkWakes between sleeps
        skew["s"] += gap
        h._bump_stale_streak(agent)
        h._check_stale_giveup(agent)  # never raises on a suspend-caused kill

    assert h._stale_streak(agent) == 0


def test_awake_stale_kills_still_trip_the_breaker(monkeypatch):
    """Control: with no suspend in between, an unresponsive provider still gives up at 5 —
    including right after a suspend-excused kill (the excuse does not carry over)."""
    import agent.chat_completion_helpers as h
    import agent.chat_completion_stale_breaker as sb

    monkeypatch.delenv("HERMES_STREAM_STALE_GIVEUP", raising=False)
    skew = {"s": 50.0}
    monkeypatch.setattr(sb, "_wall_minus_awake", lambda: skew["s"], raising=False)
    agent = SimpleNamespace()
    h._reset_stale_streak(agent)
    skew["s"] += 7_200
    h._bump_stale_streak(agent)  # the wake kill: excused
    assert h._stale_streak(agent) == 0
    for _ in range(5):
        skew["s"] += 2  # clock noise, no suspend
        h._bump_stale_streak(agent)
    assert h._stale_streak(agent) == 5
    with pytest.raises(RuntimeError, match="5 consecutive stale attempts"):
        h._check_stale_giveup(agent)


def test_breaker_without_a_baseline_counts_the_first_kill(monkeypatch):
    """No reset yet (fresh agent): there is no evidence of a suspend, so the kill counts."""
    import agent.chat_completion_helpers as h
    import agent.chat_completion_stale_breaker as sb

    monkeypatch.setattr(sb, "_wall_minus_awake", lambda: 9_999.0, raising=False)
    agent = SimpleNamespace()
    h._bump_stale_streak(agent)
    assert h._stale_streak(agent) == 1


# ── Codex + Anthropic turn failures through recovery and the dispatcher ──


@pytest.mark.parametrize(
    ("change", "state"),
    [
        pytest.param({}, "live", id="unexpired"),
        pytest.param({"claim_expires": -60}, "lapsed", id="expired-only"),
        pytest.param({"claim_expires": -60, "claim_lock": "other:1"}, "lost", id="expired-and-relocked"),
        pytest.param({"claim_expires": -60, "status": "ready"}, "lost", id="expired-and-released"),
        pytest.param({"claim_expires": None}, "lost", id="null-lease"),
    ],
)
def test_claim_state_separates_a_lapse_from_a_takeover(tmp_path, monkeypatch, change, state):
    """Only a lease whose every coordinate is still ours reads ``lapsed``; anything else is ``lost``."""
    import agent.kanban_turn_recovery as rec
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        tid = kb.create_task(conn, title="t")
        task = kb.claim_task(conn, tid, claimer="me:1")
        now = int(time.time())
        with kb.write_txn(conn):
            for column, value in change.items():
                if column == "claim_expires" and value is not None:
                    value = now + value
                conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, tid))
                if column == "claim_expires":
                    conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (value, task.current_run_id))
    got = rec.worker_claim_state(task_id=tid, db_path=str(path), run_id=str(task.current_run_id),
                                 claim_lock="me:1", verify_pid=False)
    assert got == state
    assert rec.worker_claim_is_live(task_id=tid, db_path=str(path), run_id=str(task.current_run_id),
                                    claim_lock="me:1", verify_pid=False) is (state == "live")


_CODEX_OFFLINE = {
    "failed": True, "failure_retryable": True, "failure_reason": "timeout",
    "error": "APIConnectionError: Connection error. ([Errno 8] nodename nor servname provided, or not known)",
    "final_response": "API call failed after 3 retries: Connection error.", "completed": False, "messages": [],
}
_ANTHROPIC_STALE = {
    "failed": True, "failure_retryable": True, "failure_reason": "timeout",
    "error": "Stream stalled: no data from api.anthropic.com for 180s",
    "final_response": "API call failed after 3 retries: stream stalled", "completed": False, "messages": [],
}
_SUCCESS = {"failed": False, "completed": True, "final_response": "done", "messages": []}

_SLEEP_S = 4 * 3600 + 33 * 60  # 16:57:52 -> 21:31:30


@pytest.fixture(autouse=True)
def online_network(monkeypatch):
    monkeypatch.setattr("agent.retry_utils.host_resolves", lambda *a, **k: True)


@pytest.fixture
def submitter():
    """The dispatcher-spawned submitter process: alive the whole time the owner runs turns."""
    from hermes_cli import kanban_db_dispatch as kbd

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        for _ in range(50):
            if kbd._process_fingerprint(proc.pid):
                break
            time.sleep(0.05)
        yield proc
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def slept_worker(tmp_path, monkeypatch, submitter):
    """A host-local managed run whose lease lapsed during a recorded 4.5 h sleep.

    ``install(script, on_sleep)`` returns ``(run, entered, context, conn)``; ``on_sleep(n, conn,
    context)`` runs inside every recovery wait (n = 1, 2, ...) and stands in for whatever the
    dispatcher does meanwhile."""
    import agent.kanban_turn_recovery as rec
    from gateway import session_kanban
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_suspend_ledger as ledger
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    for key, value in (("HERMES_KANBAN_TASK", "t_foreign"), ("HERMES_KANBAN_DB", str(tmp_path / "nope.db")),
                       ("HERMES_KANBAN_RUN_ID", "999"), ("HERMES_KANBAN_CLAIM_LOCK", "foreign")):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(rec, "RECOVERY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr("agent.skill_commands.build_preloaded_skills_prompt", lambda *a, **k: ("", [], []))
    opened = []

    def install(script, on_sleep=lambda n, conn, context: None):
        kb.init_db()
        path = kb.kanban_db_path()
        assert str(path).startswith(str(tmp_path))
        conn = connect(path)
        opened.append(conn)
        task_id = kb.create_task(conn, title="sleep survival", assignee="default")
        task = kb.claim_task(conn, task_id)  # host-local claimer: this dispatcher's
        assert task is not None
        kbd._set_worker_pid(conn, task_id, submitter.pid, run_id=task.current_run_id, claim_lock=task.claim_lock)
        now = int(time.time())
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound",
                             {"pid": os.getpid(), "claim_lock": task.claim_lock}, run_id=task.current_run_id)
            # Last heartbeat and lease renewal right before the lid closed; the lease ran out asleep.
            conn.execute("UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ?, started_at = ? WHERE id = ?",
                         (now - 60, now - _SLEEP_S - 30, now - _SLEEP_S - 600, task_id))
            conn.execute("UPDATE task_runs SET claim_expires = ?, last_heartbeat_at = ?, started_at = ? WHERE id = ?",
                         (now - 60, now - _SLEEP_S - 30, now - _SLEEP_S - 600, task.current_run_id))
        # The dispatcher's own record of the suspend (it ran right before and right after it).
        ledger.record_dispatch_pass(conn, now=now - _SLEEP_S, interval_seconds=60)
        ledger.record_dispatch_pass(conn, now=now - 20, interval_seconds=60)

        context = {"db": str(path), "task_id": task_id, "run_id": task.current_run_id,
                   "claim_lock": task.claim_lock, "skills": [], "context": "", "goal_mode": False,
                   "goal_text": "g", "goal_max_turns": 2}
        entered: list[str] = []
        seen = {"n": 0}
        sleeps = {"n": 0}

        def fake_sleep(_seconds):
            sleeps["n"] += 1
            on_sleep(sleeps["n"], conn, context)

        monkeypatch.setattr(rec, "_sleep", fake_sleep, raising=False)

        class _Agent:
            session_id = "s1"

            def __init__(self):
                self._session_db = self

            def get_messages_as_conversation(self, _sid):
                return [{"role": "user", "content": "from-store"}]

            def run_conversation(self, message, conversation_history=None, **_kwargs):
                entered.append(message)
                result = script[seen["n"]]
                seen["n"] += 1
                return result

        frame = {"text": "work", "policy": {"kanban_json": json.dumps(context)}}

        def run() -> int:
            session_kanban.run_worker_turns(_Agent(), frame, [])
            return session_kanban.worker_exit_code(path, context)

        return run, entered, context, conn

    yield install
    for conn in opened:
        conn.close()


def _dispatcher_tick(n, conn, context):
    """The dispatcher's first pass after the wake: the stale sweep sees a live submitter."""
    from hermes_cli import kanban_db as kb

    if n == 1:
        kb.release_stale_claims(conn)


@pytest.mark.parametrize("failure", [pytest.param(_CODEX_OFFLINE, id="codex-offline"),
                                     pytest.param(_ANTHROPIC_STALE, id="anthropic-stale")])
def test_lapsed_lease_is_renewed_and_the_run_survives(slept_worker, failure):
    """The turn fails in a DarkWake while the lease is lapsed. The worker waits for the dispatcher's
    renewal instead of exiting, then recovers in place: same run, no failure counted, no second run."""
    from hermes_cli import kanban_db as kb

    run, entered, context, conn = slept_worker([failure, _SUCCESS], on_sleep=_dispatcher_tick)

    assert run() == 0
    assert len(entered) == 2 and "Do NOT start over" in entered[1]
    events = kb.list_events(conn, context["task_id"])
    kinds = [e.kind for e in events]
    assert "claim_extended" in kinds
    assert not {"reclaimed", "crashed", "timed_out", "heartbeat_stale"} & set(kinds)
    extended = [e for e in events if e.kind == "claim_extended"][-1]
    assert extended.payload["claim_lock"] == context["claim_lock"]
    task = kb.get_task(conn, context["task_id"])
    assert task.status == "running" and task.current_run_id == context["run_id"]
    assert task.claim_lock == context["claim_lock"]
    assert (task.consecutive_failures or 0) == 0
    runs = conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (context["task_id"],)).fetchall()
    assert [r[0] for r in runs] == [context["run_id"]]  # no duplicate attempt


@pytest.mark.parametrize("failure", [pytest.param(_CODEX_OFFLINE, id="codex-offline"),
                                     pytest.param(_ANTHROPIC_STALE, id="anthropic-stale")])
def test_repeated_offline_darkwakes_preserve_the_run(slept_worker, monkeypatch, failure):
    """Each DNS recovery is followed by another offline turn, beyond the ordinary retry budget."""
    import agent.kanban_turn_recovery as rec
    import agent.retry_utils as retry
    from hermes_cli import kanban_db as kb

    further_failures = rec.DEFAULT_MAX_RECOVERY_ATTEMPTS + 2
    clock = {"awake": 0.0, "wall": time.time(), "offline": 0, "until": 0.0}

    class Outages(list):
        def __getitem__(self, index):
            result = super().__getitem__(index)
            if index:
                assert clock["awake"] >= clock["until"], "model re-entered while still offline"
            # The first outage outlasts even the per-request 900-awake-second wait.
            clock["until"] = clock["awake"] + (1000 if index == 0 else 30)
            return result

    def resolve(_host, **_kwargs):
        online = clock["awake"] >= clock["until"]
        clock["offline"] += not online
        return online

    def darkwake(seconds):
        clock["awake"] += seconds
        clock["wall"] += seconds + 3600  # an hour asleep does not spend awake time

    def wake(n, conn, context):
        _dispatcher_tick(n, conn, context)
        darkwake(15)

    monkeypatch.setattr(rec, "_awake_clock", lambda: clock["awake"])
    monkeypatch.setattr(retry, "host_resolves", resolve)
    monkeypatch.setattr(retry, "_OFFLINE_SLICE_S", 30)
    # Isolate fake clocks from subprocess/SQLite clocks. Only the DNS boundary and sleep
    # are fake; real claim checks and dispatcher renewal still run against the fixture DB.
    monkeypatch.setattr(retry, "time", SimpleNamespace(
        monotonic=lambda: clock["awake"], time=lambda: clock["wall"], sleep=darkwake,
    ))
    run, entered, context, conn = slept_worker(
        Outages([failure] * (1 + further_failures) + [_SUCCESS]), on_sleep=wake,
    )

    assert run() == 0
    assert len(entered) == further_failures + 2
    assert clock["offline"] >= further_failures
    task = kb.get_task(conn, context["task_id"])
    assert task.status == "running" and task.current_run_id == context["run_id"]
    assert task.claim_lock == context["claim_lock"]
    assert (task.consecutive_failures or 0) == 0
    runs = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (context["task_id"],)).fetchall()
    assert [row[0] for row in runs] == [context["run_id"]]
    assert not {"reclaimed", "crashed", "timed_out", "heartbeat_stale"} & {
        event.kind for event in kb.list_events(conn, context["task_id"])
    }


def test_offline_claim_loss_does_not_spend_an_attempt(monkeypatch):
    import agent.kanban_turn_recovery as rec
    import agent.retry_utils as retry

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_offline")
    monkeypatch.delenv("HERMES_KANBAN_TURN_RECOVERY", raising=False)
    live = {"value": True}

    def resolve(_host, **_kwargs):
        live["value"] = False
        return False

    monkeypatch.setattr(retry, "host_resolves", resolve)
    turns, sleeps = [], []
    attempts = rec.recover_failed_kanban_turns(
        turns.append, lambda: _CODEX_OFFLINE, claim_check=lambda: live["value"],
        sleep_fn=sleeps.append,
    )
    assert attempts == 0
    assert turns == []
    assert sleeps == []


@pytest.mark.parametrize("failure,online,expected", [
    pytest.param(_CODEX_OFFLINE, True, 3, id="online-codex-is-bounded"),
    pytest.param(_ANTHROPIC_STALE, True, 3, id="online-anthropic-is-bounded"),
    pytest.param({**_CODEX_OFFLINE, "failure_reason": "server_error", "error": "HTTP 500"},
                 False, 3, id="non-network-never-probes"),
    pytest.param({**_CODEX_OFFLINE, "failure_reason": "billing"}, False, 0, id="billing-never-probes"),
    pytest.param({**_CODEX_OFFLINE, "interrupted": True}, False, 0, id="cancelled-never-probes"),
    pytest.param({**_CODEX_OFFLINE, "turn_exit_reason": "max_iterations_reached(5/5)"},
                 False, 0, id="settled-never-probes"),
])
def test_offline_gate_preserves_normal_retry_authority(monkeypatch, failure, online, expected):
    import agent.kanban_turn_recovery as rec
    import agent.retry_utils as retry

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.setenv("HERMES_KANBAN_TURN_RECOVERY", "3")

    def resolve(_host, **_kwargs):
        assert online, "a non-network or unauthorized failure probed DNS"
        return True

    monkeypatch.setattr(retry, "host_resolves", resolve)
    turns = []
    attempts = rec.recover_failed_kanban_turns(
        turns.append, lambda: failure, claim_check=lambda: True, sleep_fn=lambda _seconds: None,
    )
    assert attempts == expected
    assert len(turns) == expected


@pytest.mark.parametrize("steal", ["reclaim", "relock"])
def test_offline_wait_still_fences_a_stolen_run(slept_worker, monkeypatch, steal):
    import agent.retry_utils as retry

    run, entered, context, conn = slept_worker([_CODEX_OFFLINE, _SUCCESS], on_sleep=_dispatcher_tick)

    def resolve(_host, **_kwargs):
        (_steal if steal == "reclaim" else _relock_in_place)(1, conn, context)
        return False

    monkeypatch.setattr(retry, "host_resolves", resolve)
    assert run() == KANBAN_RATE_LIMIT_EXIT_CODE
    assert len(entered) == 1


def test_mixed_offline_and_online_failures_only_charge_online_attempts(monkeypatch):
    import agent.kanban_turn_recovery as rec
    import agent.retry_utils as retry

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_mixed")
    monkeypatch.setenv("HERMES_KANBAN_TURN_RECOVERY", "3")
    clock = {"awake": 0.0, "until": 5.0}
    charged = []

    def turn(_nudge):
        # Alternate a real offline wait with an online timeout; never succeed.
        charged.append(clock["until"] == 0)
        clock["until"] = clock["awake"] + 5 if charged[-1] else 0

    monkeypatch.setattr(retry, "host_resolves", lambda *a, **k: clock["awake"] >= clock["until"])
    monkeypatch.setattr(retry, "time", SimpleNamespace(
        monotonic=lambda: clock["awake"],
        sleep=lambda seconds: clock.__setitem__("awake", clock["awake"] + seconds),
    ))
    attempts = rec.recover_failed_kanban_turns(
        turn, lambda: _CODEX_OFFLINE, claim_check=lambda: True, sleep_fn=lambda _seconds: None,
    )
    assert attempts == sum(charged) == 3
    assert charged.count(False) > rec.DEFAULT_MAX_RECOVERY_ATTEMPTS


@pytest.mark.parametrize("dns", ["blocked-control", "single-blip"])
def test_one_failed_control_lookup_is_not_an_offline_exemption(monkeypatch, dns):
    import agent.kanban_turn_recovery as rec
    import agent.retry_utils as retry

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    probes = []

    def resolve(host, **_kwargs):
        probes.append(host)
        if dns == "blocked-control":
            if len(probes) > 5:
                pytest.fail("waiting on one blocked control despite working DNS")
            return host != retry.NETWORK_CONTROL_HOST
        return len(probes) > 1

    monkeypatch.setattr(retry, "host_resolves", resolve)
    # A blocked-control regression should fail immediately, not sleep on the real network.
    monkeypatch.setattr(retry, "time", SimpleNamespace(monotonic=lambda: 0,
        sleep=lambda _s: pytest.fail("unnecessary offline wait")))
    result = {"value": _CODEX_OFFLINE}
    attempts = rec.recover_failed_kanban_turns(
        lambda _n: result.update(value=_SUCCESS), lambda: result["value"],
        claim_check=lambda: True, sleep_fn=lambda _seconds: None,
    )
    assert attempts == 1


def _steal(n, conn, context):
    """Reclaim fences the old worker; missing accounting also fences another paid claim."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_sync import pop_claim_deferral

    if n == 1:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
                         "worker_pid=NULL, worker_started_at=NULL WHERE id=?", (context["task_id"],))
            kb._end_run(conn, context["task_id"], outcome="reclaimed", status="reclaimed", error="stolen")
        assert kb.claim_task(conn, context["task_id"], claimer="other-gateway:4242") is None
        assert pop_claim_deferral(context['task_id']) == 'usage_accounting_pending'


def _relock_in_place(n, conn, context):
    """The lease is renewed, but under a different lock: not ours."""
    from hermes_cli import kanban_db as kb

    if n == 1:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_lock='other-gateway:4242', claim_expires=? WHERE id=?",
                         (int(time.time()) + 900, context["task_id"]))


@pytest.mark.parametrize("steal", [pytest.param(_steal, id="reclaimed-accounting-pending"),
                                   pytest.param(_relock_in_place, id="renewed-under-another-lock")])
def test_stolen_lease_stays_fenced(slept_worker, steal):
    """A lapse is only waited out for OUR lease: once anyone else holds the card the worker never
    re-enters the model, and the other holder's run is left alone."""
    from hermes_cli import kanban_db as kb

    run, entered, context, conn = slept_worker([_CODEX_OFFLINE, _SUCCESS], on_sleep=steal)

    assert run() == KANBAN_RATE_LIMIT_EXIT_CODE
    assert len(entered) == 1  # no recovery turn under a lease that is no longer ours
    task = kb.get_task(conn, context["task_id"])
    assert task.claim_lock == (None if steal is _steal else "other-gateway:4242")


def test_lease_never_renewed_gives_up_after_the_awake_budget(slept_worker, monkeypatch):
    """Nobody renews (dispatcher gone): the wait is bounded by AWAKE time and ends without a turn."""
    import agent.kanban_turn_recovery as rec

    awake = {"t": 0.0}
    monkeypatch.setattr(rec, "_awake_clock", lambda: awake["t"])

    def nobody(n, conn, context):
        awake["t"] += rec.LAPSED_CLAIM_POLL_SECONDS

    run, entered, _context, _conn = slept_worker([_CODEX_OFFLINE, _SUCCESS], on_sleep=nobody)

    assert run() == KANBAN_RATE_LIMIT_EXIT_CODE
    assert len(entered) == 1
    assert awake["t"] >= rec.LAPSED_CLAIM_WAIT_SECONDS


# ── the dispatcher books the worker's own transient exit ─────────────


@pytest.fixture
def dead_submitter():
    from hermes_cli import kanban_db_dispatch as kbd

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    fingerprint = None
    for _ in range(50):
        fingerprint = kbd._process_fingerprint(proc.pid)
        if fingerprint:
            break
        time.sleep(0.05)
    proc.kill()
    proc.wait()
    return proc.pid, fingerprint


def _exited_run(conn, pid, fingerprint, *, exit_code):
    """A host-local run whose worker already exited (``exit_code`` booked by its owner, None = no
    booking) and whose lease lapsed while the host slept."""
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(conn, title="exited", assignee="default")
    task = kb.claim_task(conn, tid)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=?, worker_started_at=?, claim_expires=?, last_heartbeat_at=?, "
                     "started_at=? WHERE id=?", (pid, fingerprint, now - 60, now - 120, now - 3600, tid))
        conn.execute("UPDATE task_runs SET worker_pid=?, worker_started_at=?, claim_expires=? WHERE id=?",
                     (pid, fingerprint, now - 60, task.current_run_id))
        kb._append_event(conn, tid, "worker_bound", {"pid": pid, "claim_lock": task.claim_lock},
                         run_id=task.current_run_id)
        if exit_code is not None:
            kb._append_event(conn, tid, "worker_result", {"pid": pid, "claim_lock": task.claim_lock,
                                                          "exit_code": exit_code, "last_output": ""},
                             run_id=task.current_run_id)
    return tid, task.current_run_id


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def test_transient_exit_is_booked_by_the_crash_sweep_not_as_a_failure(board, dead_submitter):
    """The worker exited 75 (transient: requeue without a failure) and its lease lapsed. The stale
    sweep leaves that booking to the crash sweep of the same pass: no failure, run ``rate_limited``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    tid, run_id = _exited_run(board, *dead_submitter, exit_code=KANBAN_RATE_LIMIT_EXIT_CODE)

    assert kb.release_stale_claims(board) == 0
    assert "reclaimed" not in [e.kind for e in kb.list_events(board, tid)]
    kbd.detect_crashed_workers(board)

    outcome = board.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]
    assert outcome == "rate_limited"
    task = kb.get_task(board, tid)
    assert task.status == "ready" and (task.consecutive_failures or 0) == 0


def test_exit_without_a_booking_is_still_reclaimed(board, dead_submitter):
    """Control: a dead worker that booked nothing is reclaimed by the stale sweep exactly as before."""
    from hermes_cli import kanban_db as kb

    tid, run_id = _exited_run(board, *dead_submitter, exit_code=None)

    assert kb.release_stale_claims(board) == 1
    outcome = board.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]
    assert outcome == "reclaimed"
    assert (kb.get_task(board, tid).consecutive_failures or 0) == 1
