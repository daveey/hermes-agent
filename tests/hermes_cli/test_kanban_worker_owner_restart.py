"""A gateway restart never kills or charges the Kanban runs it is waiting on. [daveey]

2026-10-30, hermes-hub: ``hermes update`` asked the gateway to restart with 16 managed Kanban runs in
flight. Its after-turn wait (``agent.restart_after_turn_timeout``, 1800 s) cut each run's submitter
socket and refused the redial as ``draining``/``restart_wait``, and the submitter counted that refusal
as owner silence. 180 s in (``OWNER_WAIT_S``) all 11 still running gave up ("Kanban owner silent for
192s: Gateway draining: restart_wait"), their exit made ``_watch_submitter`` end the very turns the
wait was waiting on (``Managed worker lost: EOFError``), and the dead-worker sweep booked 11 identical
crashes as systemic: ``gave_up`` to triage on the first failure.

* A submitter waits out an owner that keeps answering ``restart_wait`` for as long as that owner's
  own after-turn cap, then gives up only past it.
* A submitter whose attempt is lost to the owner's restart exits ``KANBAN_OWNER_RESTART_EXIT_CODE``,
  and the sweep requeues it as ``reclaimed`` without counting a failure, however many die together.
"""

import asyncio
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.drain_control import RESTART_WAIT_REASON
from hermes_cli import gateway_client, kanban_worker_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE

GUE = gateway_client.GatewayUnavailableError


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    monkeypatch.setenv("HERMES_RESTART_AFTER_TURN_TIMEOUT", "1800")


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(kanban_worker_client, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def _sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(kanban_worker_client.asyncio, "sleep", _sleep)
    return now


class _Owner:
    """First connection admits the claim (pending); the restart then cuts it and refuses every
    redial with ``refusal`` until ``wait_ends`` (``None``: never). After that a new owner answers
    the receipt ``final``."""

    def __init__(self, clock, wait_ends, final="terminal", refusal=("draining", RESTART_WAIT_REASON)):
        self.clock, self.wait_ends, self.final, self.refusal = clock, wait_ends, final, refusal
        self.dials = 0

    def connect(self):
        owner = self

        @asynccontextmanager
        async def _connect(*_, **__):
            owner.dials += 1
            first = owner.dials == 1
            in_wait = not first and (owner.wait_ends is None or owner.clock[0] < owner.wait_ends)
            if in_wait:
                if owner.refusal is None:
                    raise ConnectionRefusedError("owner gone")
                state, reason = owner.refusal
                raise GUE(f"Gateway {state}: {reason}", state, reason)

            class _Client:
                events = asyncio.Queue()

                async def rpc(self, method, **params):
                    if method == "kanban.run":
                        return {"session_id": "s1", "receipt": {"status": "pending", "admission_id": "a1"}}
                    if first:
                        raise gateway_client.GatewayClientError("Gateway disconnected; turn outcome is unknown")
                    return {"status": owner.final}

            yield _Client()

        return _connect


_RESTARTING = [("draining", RESTART_WAIT_REASON), ("draining", "update_paused"), ("starting", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", _RESTARTING)
async def test_a_worker_outlasts_a_restart_wait_longer_than_owner_wait(monkeypatch, clock, tmp_path, refusal):
    # The turn runs 10 min into the drain (> OWNER_WAIT_S), then the next owner holds its receipt.
    owner = _Owner(clock, wait_ends=600.0, refusal=refusal)
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())
    monkeypatch.setattr(kanban_worker_client, "_exit_code", lambda receipt, params, db: 0)
    assert await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db")) == 0
    assert clock[0] >= 600.0 > kanban_worker_client.OWNER_WAIT_S


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", _RESTARTING)
async def test_a_restart_wait_past_its_own_cap_ends_the_worker_as_an_owner_restart(
        monkeypatch, clock, tmp_path, refusal):
    owner = _Owner(clock, wait_ends=None, refusal=refusal)
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())
    with pytest.raises(kanban_worker_client.OwnerRestartLost, match="owner silent"):
        await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))
    assert clock[0] >= 1800.0 + kanban_worker_client.OWNER_WAIT_S - 20  # the last refresh lands up to one backoff early
    # Bounded: it never waits on a wedged owner much past the owner's own cap.
    assert clock[0] < 1800.0 + kanban_worker_client.OWNER_WAIT_S + 60


@pytest.mark.asyncio
async def test_an_owner_silent_with_no_draining_answer_still_fails_at_owner_wait(monkeypatch, clock, tmp_path):
    owner = _Owner(clock, wait_ends=None, refusal=None)  # refused dials only: no owner says anything
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())
    with pytest.raises(gateway_client.GatewayClientError, match="owner silent") as raised:
        await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))
    assert not isinstance(raised.value, kanban_worker_client.OwnerRestartLost)
    assert kanban_worker_client.OWNER_WAIT_S <= clock[0] < 2 * kanban_worker_client.OWNER_WAIT_S


@pytest.mark.asyncio
async def test_a_turn_the_restart_killed_reads_as_an_owner_restart(monkeypatch, clock, tmp_path):
    # Force drain at the cap ended the turn: the next owner answers ``unknown``.
    owner = _Owner(clock, wait_ends=60.0, final="unknown")
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())
    with pytest.raises(kanban_worker_client.OwnerRestartLost):
        await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))


@pytest.mark.asyncio
async def test_an_unknown_turn_with_no_restart_stays_a_failure(monkeypatch, clock, tmp_path):
    class _Client:
        events = asyncio.Queue()

        async def rpc(self, method, **params):
            return {"session_id": "s1", "receipt": {"status": "unknown", "admission_id": "a1"}}

    @asynccontextmanager
    async def _connect(*_, **__):
        yield _Client()
    monkeypatch.setattr(gateway_client, "connect_gateway", _connect)
    with pytest.raises(gateway_client.GatewayClientError) as raised:
        await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))
    assert not isinstance(raised.value, kanban_worker_client.OwnerRestartLost)


def test_main_exits_with_the_owner_restart_code(monkeypatch, tmp_path, capsys):
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": "t1", "HERMES_KANBAN_RUN_ID": "1",
                       "HERMES_KANBAN_CLAIM_LOCK": "h:1", "HERMES_KANBAN_DB": str(tmp_path / "kanban.db")}.items():
        monkeypatch.setenv(key, value)

    async def _lost(params, board_db):
        raise kanban_worker_client.OwnerRestartLost("Kanban owner silent for 1990s: Gateway draining: restart_wait")
    monkeypatch.setattr(kanban_worker_client, "run", _lost)
    assert kanban_worker_client.main() == KANBAN_OWNER_RESTART_EXIT_CODE
    assert f"rc={KANBAN_OWNER_RESTART_EXIT_CODE}" in capsys.readouterr().err


# --- dispatcher: the sweep books an owner-restart exit as a reclaim, never a crash --------------------

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *a, **kw: "")
    return tmp_path


def _running(conn, title, pid):
    tid = kb.create_task(conn, title=title, assignee="default")
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?", (pid, int(time.time()) - 600, tid))
    return tid


def _marked(tid, text):
    """Prefix the task's current run start marker, as the dispatcher writes before spawning."""
    with kbc.connect() as c:
        rid = c.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return f"[kanban-worker-start] run_id={rid}\n{text}"


def _reaped(monkeypatch, codes):
    exits = {pid: ((code & 0xFF) << 8, time.time()) for pid, code in codes.items()}
    monkeypatch.setattr(kbd, "_recent_worker_exits", exits)


def test_eleven_workers_lost_to_one_restart_are_all_requeued_uncounted(board, monkeypatch):
    with kbc.connect() as conn:
        pids = list(range(72001, 72012))
        tids = [_running(conn, f"card {pid}", pid) for pid in pids]
        _reaped(monkeypatch, {pid: KANBAN_OWNER_RESTART_EXIT_CODE for pid in pids})
        assert kbd.detect_crashed_workers(conn) == []
        assert kbd.detect_crashed_workers._last_auto_blocked == []  # type: ignore[attr-defined]
        assert sorted(kbd.detect_crashed_workers._last_owner_restarted) == sorted(tids)  # type: ignore[attr-defined]
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "ready" and task.consecutive_failures == 0
            outcome, error = conn.execute(
                "SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            assert outcome == "reclaimed" and "gateway restart" in error
            kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
            assert "crashed" not in kinds and "gave_up" not in kinds
            assert kinds.count("owner_restart_lost") == 1
            # Spaced like a quota requeue, so it does not respawn into the same drain.
            assert kbd.check_respawn_guard(conn, tid) == "owner_restart_cooldown"
        assert kbd._protocol_violation_streak(conn, tids[0]) == 0


def test_a_requeued_owner_restart_respawns_after_its_cooldown(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "0")
    with kbc.connect() as conn:
        tid = _running(conn, "card", 74001)
        _reaped(monkeypatch, {74001: KANBAN_OWNER_RESTART_EXIT_CODE})
        kbd.detect_crashed_workers(conn)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_same_error_ordinary_crashes_still_trip_the_systemic_hold(board, monkeypatch):
    with kbc.connect() as conn:
        pids = [75001, 75002, 75003]
        tids = [_running(conn, f"card {pid}", pid) for pid in pids]
        _reaped(monkeypatch, {pid: 1 for pid in pids})
        assert sorted(kbd.detect_crashed_workers(conn)) == sorted(tids)
        for tid in tids:
            kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
            assert "gave_up" in kinds


def test_a_restart_lost_exit_booked_by_the_next_gateway_is_requeued_too(board, monkeypatch):
    # 2026-10-30 David-MBP: the draining gateway never reaped its workers; the next gateway process
    # booked their exits ~100 s later from the worker log's exit trailer, as plain crashes.
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    trailer = f"Kanban owner restarted: ...\n{KANBAN_WORKER_EXIT_TRAILER}{KANBAN_OWNER_RESTART_EXIT_CODE}\n"
    monkeypatch.setattr(kb, "read_worker_log", lambda tid, **kw: _marked(tid, trailer))
    with kbc.connect() as conn:
        tids = [_running(conn, f"card {pid}", pid) for pid in (76001, 76002, 76003)]
        assert kbd.detect_crashed_workers(conn) == []
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "ready" and task.consecutive_failures == 0
            kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
            assert "owner_restart_lost" in kinds and "gave_up" not in kinds and "crashed" not in kinds


def test_a_plain_nonzero_exit_is_still_a_crash(board, monkeypatch):
    with kbc.connect() as conn:
        tid = _running(conn, "card", 73001)
        _reaped(monkeypatch, {73001: 1})
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_owner_restart_is_its_own_exit_not_the_never_admitted_one():
    # 69 = never admitted (kanban_owner_unavailable, streak breaker); 76 = an admitted turn the
    # restart took. One code for both would book an admitted loss as a crash.
    from hermes_cli.kanban_db_dispatch_exit_code import exit_code_kind
    from hermes_cli.kanban_owner_unavailable import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
    assert KANBAN_OWNER_RESTART_EXIT_CODE != KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
    assert exit_code_kind(KANBAN_OWNER_RESTART_EXIT_CODE)[0] == "owner_restart"
    assert exit_code_kind(KANBAN_OWNER_UNAVAILABLE_EXIT_CODE)[0] == "owner_unavailable"


def test_restart_losses_in_a_row_on_one_card_never_block_it(board, monkeypatch):
    # t_01a18bf8fc6d1564 (c): repeated drains each take one run of the same card.
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "0")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default", max_retries=2)
        host = kb._claimer_id().split(":", 1)[0]
        for i, pid in enumerate((77001, 77002, 77003)):
            kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?",
                             (pid, int(time.time()) - 600, tid))
            _reaped(monkeypatch, {pid: KANBAN_OWNER_RESTART_EXIT_CODE})
            assert kbd.detect_crashed_workers(conn) == []
            task = kb.get_task(conn, tid)
            assert task is not None
            assert (task.status, task.consecutive_failures, task.block_kind) == ("ready", 0, None), i
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert kinds.count("owner_restart_lost") == 3
        assert "crashed" not in kinds and "gave_up" not in kinds


_LEGACY = "Kanban owner refused: Kanban owner silent for 184s: Gateway draining: restart_wait"


def _legacy_log(monkeypatch, text):
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    log = f"{text}\n{KANBAN_WORKER_EXIT_TRAILER}1\n"
    monkeypatch.setattr(kb, "read_worker_log", lambda tid, **kw: _marked(tid, log))
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})


def test_a_pre_fix_worker_refused_by_the_drain_after_admission_is_an_owner_restart(board, monkeypatch):
    # The transition drain: workers in flight when the update installing this fix restarts the
    # gateway still run the old client, which exits 1 with this line (t_01a188eb959227ce 7643/7665).
    _legacy_log(monkeypatch, _LEGACY)
    with kbc.connect() as conn:
        tid = _running(conn, "card", 78001)
        run_id = kb.get_task(conn, tid).current_run_id
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "owner_admitted", {}, run_id=run_id)
        from hermes_cli import kanban_owner_recovery
        monkeypatch.setattr(kanban_owner_recovery, "owner_reclaim_blocked", lambda *a, **k: False)
        assert kbd.detect_crashed_workers(conn) == []
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]
        assert outcome == "reclaimed"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "owner_restart_lost" in kinds and "crashed" not in kinds and "gave_up" not in kinds


def test_a_pre_fix_worker_refused_before_admission_is_owner_unavailable(board, monkeypatch):
    _legacy_log(monkeypatch, _LEGACY)
    with kbc.connect() as conn:
        tid = _running(conn, "card", 78101)
        assert kbd.detect_crashed_workers(conn) == []
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        outcome = conn.execute("SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                               (tid,)).fetchone()[0]
        assert outcome == "owner_unavailable"


def test_an_exit_1_whose_last_line_is_not_the_drain_refusal_is_still_a_crash(board, monkeypatch):
    _legacy_log(monkeypatch, _LEGACY + "\nTraceback (most recent call last): boom")
    with kbc.connect() as conn:
        tid = _running(conn, "card", 78201)
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1


# --- end to end: the contract hermes-sync's ``GATEWAY_RESTART_SAFE`` declaration promises -------------

@pytest.mark.parametrize("booked_by", ["reaped_exit", "log_trailer"])
def test_a_worker_mid_turn_at_an_owner_restart_is_requeued_uncounted_end_to_end(
        board, monkeypatch, clock, capsys, booked_by):
    """The real worker entry point (``main`` -> ``_submit`` -> ``run``) mid-turn when its owner restarts:
    the owner refuses every redial ``draining``/``restart_wait`` for longer than ``OWNER_WAIT_S``, then
    a new owner reports the turn ``unknown``. The worker exits ``KANBAN_OWNER_RESTART_EXIT_CODE``, and
    the dead-worker sweep, booking that exit from the reaped status or from the worker log's exit
    trailer, puts the card back to ready with no failure counted. [daveey]"""
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    with kbc.connect() as conn:
        tid = _running(conn, "card mid-turn at the restart", 79001)
        claimed = kb.get_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
        claim_lock = conn.execute("SELECT claim_lock FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": tid,
                       "HERMES_KANBAN_RUN_ID": str(run_id), "HERMES_KANBAN_CLAIM_LOCK": claim_lock,
                       "HERMES_KANBAN_DB": str(board / "kanban.db")}.items():
        monkeypatch.setenv(key, value)
    owner = _Owner(clock, wait_ends=600.0, final="unknown")  # draining 10 min (> OWNER_WAIT_S), then unknown
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())

    rc = kanban_worker_client.main()

    assert rc == KANBAN_OWNER_RESTART_EXIT_CODE
    assert clock[0] >= 600.0 > kanban_worker_client.OWNER_WAIT_S  # the drain's refusals kept it alive
    log = capsys.readouterr().err
    assert log.rstrip().endswith(f"{KANBAN_WORKER_EXIT_TRAILER}{KANBAN_OWNER_RESTART_EXIT_CODE}")
    # ``_submit`` strips HERMES_KANBAN_* from the worker's env; the sweep runs in the dispatcher's.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    if booked_by == "reaped_exit":
        _reaped(monkeypatch, {79001: rc})
    else:
        monkeypatch.setattr(kbd, "_recent_worker_exits", {})
        monkeypatch.setattr(kb, "read_worker_log", lambda task_id, **kw: _marked(task_id, log))

    with kbc.connect() as conn:
        assert kbd.detect_crashed_workers(conn) == []
        task = kb.get_task(conn, tid)
        assert task is not None
        assert (task.status, task.consecutive_failures, task.block_kind) == ("ready", 0, None)
        outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]
        assert outcome == "reclaimed"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert kinds.count("owner_restart_lost") == 1
        assert "crashed" not in kinds and "gave_up" not in kinds
        assert kbd._protocol_violation_streak(conn, tid) == 0


# hermes-sync's ``_RESTART_SAFE_RE`` (kanban_workers_restart_safe), verbatim.
_SYNC_RESTART_SAFE_RE = re.compile(r"^GATEWAY_RESTART_SAFE\s*(?::\s*bool\s*)?=\s*True\b", re.MULTILINE)


def test_the_client_declares_gateway_restart_safe_as_hermes_sync_reads_it():
    """hermes-sync reads this file's text with that regex; without a match every core-update drain holds
    new Kanban claims for up to 20 min. [daveey]"""
    source = Path(kanban_worker_client.__file__).read_text(encoding="utf-8")
    assert _SYNC_RESTART_SAFE_RE.search(source)
    assert kanban_worker_client.GATEWAY_RESTART_SAFE is True
