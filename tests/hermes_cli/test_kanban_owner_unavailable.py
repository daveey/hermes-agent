"""A worker whose profile owner never answers its claim is requeued uncounted, not booked a crash. [daveey]

t_82f0fe15 runs 7630/7649 (2026-10-30 08:30-08:37Z): the hub gateway sat in a restart's
``restart_wait`` drain, so each dispatcher worker waited ``OWNER_WAIT_S`` for an owner that never
admitted the claim, printed ``Kanban owner refused: Kanban owner silent for 185s: Gateway draining:
restart_wait`` and exited 1. Both were booked ``crashed``; failure_limit=2 sent the card to triage
although no turn ever ran. The next run after the restart (7690) parked cleanly.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.drain_control import RESTART_WAIT_REASON
from hermes_cli import gateway_client, kanban_worker_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_owner_unavailable as ou

GUE = gateway_client.GatewayUnavailableError


# ---------------------------------------------------------------------------
# Worker client: the exit code.
# ---------------------------------------------------------------------------


def _fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(kanban_worker_client, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def _sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(kanban_worker_client.asyncio, "sleep", _sleep)


def _owner(*, admit_first: bool):
    """Every dial lands in a restart wait, except (``admit_first``) one that admits the claim first."""
    dials = [0]

    @asynccontextmanager
    async def _connect(*_, **__):
        dials[0] += 1
        if not (admit_first and dials[0] == 1):
            raise GUE(f"Gateway draining: {RESTART_WAIT_REASON}", "draining", RESTART_WAIT_REASON)

        class _Client:
            events = asyncio.Queue()

            async def rpc(self, method, **params):
                if method == "kanban.run":
                    return {"session_id": "s1", "receipt": {"status": "pending", "admission_id": "a1"}}
                raise gateway_client.GatewayClientError("Gateway disconnected; turn outcome is unknown")
        yield _Client()
    return _connect


def _submit_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": "t1", "HERMES_KANBAN_RUN_ID": "7",
                       "HERMES_KANBAN_CLAIM_LOCK": "h:1", "HERMES_KANBAN_DB": str(tmp_path / "kanban.db")}.items():
        monkeypatch.setenv(key, value)


def test_owner_that_never_admits_exits_owner_unavailable(monkeypatch, tmp_path, capsys):
    _fake_clock(monkeypatch)
    _submit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(gateway_client, "connect_gateway", _owner(admit_first=False))
    assert kanban_worker_client._submit() == ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE == 69
    assert "Kanban owner refused: Kanban owner silent for" in capsys.readouterr().err


def test_owner_that_admitted_then_went_silent_still_exits_1(monkeypatch, tmp_path):
    _fake_clock(monkeypatch)
    _submit_env(monkeypatch, tmp_path)
    monkeypatch.setattr(gateway_client, "connect_gateway", _owner(admit_first=True))
    assert kanban_worker_client._submit() == 1


def test_a_final_owner_refusal_still_exits_1(monkeypatch, tmp_path):
    _submit_env(monkeypatch, tmp_path)

    @asynccontextmanager
    async def _connect(*_, **__):
        raise GUE("Gateway refused: unsafe peer", "inaccessible", "unsafe_control_peer")
        yield
    monkeypatch.setattr(gateway_client, "connect_gateway", _connect)
    assert kanban_worker_client._submit() == 1


# ---------------------------------------------------------------------------
# Dispatcher: the booking.
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *a, **kw: "")
    kb.init_db()
    return home


def _exits(monkeypatch, code):
    from hermes_cli.kanban_db_dispatch_exit_code import exit_code_kind
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: exit_code_kind(code))


def _running(conn, tid=None, pid=71001):
    tid = tid or kb.create_task(conn, title="goal card", assignee="default", goal_mode=True)
    kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w{pid}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    return tid


def _last_run(conn, tid):
    return conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()


def test_owner_unavailable_exit_requeues_uncounted(home, monkeypatch):
    _exits(monkeypatch, ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE)
    with kbc.connect() as conn:
        tid = _running(conn)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        run = _last_run(conn, tid)
        assert run["outcome"] == "owner_unavailable"
        assert "no turn ran" in run["error"]
        assert task.status == "ready" and task.consecutive_failures == 0


def test_two_owner_unavailable_runs_do_not_spend_the_failure_budget(home, monkeypatch):
    """The t_82f0fe15 sequence: two refusals in a row under failure_limit=2 must leave it dispatchable."""
    _exits(monkeypatch, ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE)
    with kbc.connect() as conn:
        tid = _running(conn)
        kbd.detect_crashed_workers(conn)
        _running(conn, tid, pid=71002)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        outcomes = [r["outcome"] for r in conn.execute("SELECT outcome FROM task_runs WHERE task_id=?", (tid,))]
        assert outcomes == ["owner_unavailable", "owner_unavailable"]


def test_owner_unavailable_after_an_admission_is_still_a_crash(home, monkeypatch):
    _exits(monkeypatch, ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE)
    with kbc.connect() as conn:
        tid = _running(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "owner_admitted", {"db": "/nowhere"}, run_id=run_id)
        from hermes_cli import kanban_owner_recovery
        monkeypatch.setattr(kanban_owner_recovery, "owner_reclaim_blocked", lambda *a, **k: False)
        kbd.detect_crashed_workers(conn)
        run = _last_run(conn, tid)
        assert run["outcome"] == "crashed"
        assert "the owner had admitted this run" in run["error"]
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_an_owner_that_never_returns_reaches_the_breaker(home, monkeypatch):
    _exits(monkeypatch, ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE)
    with kbc.connect() as conn:
        tid = _running(conn)
        for i in range(ou.NEUTRAL_STREAK_LIMIT):
            kbd.detect_crashed_workers(conn)
            _running(conn, tid, pid=72000 + i)
        kbd.detect_crashed_workers(conn)
        run = _last_run(conn, tid)
        assert run["outcome"] == "crashed"
        assert f"{ou.NEUTRAL_STREAK_LIMIT} runs in a row found no owner" in run["error"]
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_plain_exit_1_is_still_a_crash(home, monkeypatch):
    _exits(monkeypatch, 1)
    with kbc.connect() as conn:
        tid = _running(conn)
        kbd.detect_crashed_workers(conn)
        assert _last_run(conn, tid)["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1
