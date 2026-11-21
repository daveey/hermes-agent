"""A host fault that refuses every card's worker before a turn runs is not the card's failure. [daveey]

hermes-hub 2026-11-01: a host config outage emptied ``model_routing.routes``. Each managed Kanban
worker the owner admitted then died in ``_construct_agent`` with ``ManagedPinRejectedError`` (no pin
can pass an empty registry), was booked ``crashed`` and spent its card's failure budget although no
turn ran and no card was at fault. The owner-unavailable neutral requeue refused these runs because
the owner had admitted them. The same outage made the owner refuse other claims with
``storage_unavailable`` before admitting them (requeued uncounted since 346d92ff665, but silently).

Contract pinned here:

- a startup refusal proven (typed, by the bound interpreter, for this exact run and claim) to come
  from the host's routing registry (empty, or unreadable/invalid in ``managed_config``) requeues the
  card uncounted, however often, up to the existing ``NEUTRAL_STREAK_LIMIT`` breaker;
- a genuinely invalid pin (registry nonempty), any other routing error, a refusal after the agent
  started, or a proof written by anyone but the bound interpreter still counts;
- the host fault (startup registry refusal or a pre-admission ``storage_unavailable`` refusal) is
  surfaced on ONE host card per window, not once per card; a plain silent owner (restart drain) is not.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_owner_unavailable as ou

PIN = ("claude-opus-5-5", "anthropic")
NOTICE_PREFIX = "host-refusal:"


def _registry(routes):
    return {"model_routing": {"enabled": True, "enforce_subscription_only": True, "routes": routes}}


EMPTY_REGISTRY = _registry({})
POPULATED_REGISTRY = _registry({"deep": [{
    "provider": "anthropic", "model": "claude-sonnet-5-5", "billing": "subscription", "weight": 1,
    "capabilities": {}, "eligible": True, "funding_verified": True}]})


def _pin_error(config):
    from agent.model_routing_managed import ManagedPinRejectedError, pin_policy
    with pytest.raises(ManagedPinRejectedError) as info:
        pin_policy(*PIN, config)
    return info.value


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *a, **kw: "")
    # The bound interpreter is this test process (``_record_worker_result`` writes os.getpid()), which
    # is alive; in production it has exited by the time the sweep runs.
    from hermes_cli import kanban_owner_recovery
    monkeypatch.setattr(kanban_owner_recovery, "owner_reclaim_blocked", lambda *a, **k: False)
    from gateway import session_worker_construct
    monkeypatch.setattr(session_worker_construct, "worker_route", lambda policy, kwargs: {})
    kb.init_db()
    return home


def _admitted_run(conn, tid=None, *, submitter=71001):
    """A managed attempt the owner admitted and whose interpreter bound itself: the frame it runs with."""
    tid = tid or kb.create_task(conn, title="card", assignee="default")
    kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w{submitter}")
    task = kb.get_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (submitter, tid))
        kb._append_event(conn, tid, "owner_admitted", {"db": "/owner.db", "session_id": "s", "request_id": "r"},
                         run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_bound", {"pid": os.getpid(), "claim_lock": task.claim_lock,
                                                     "started_at": "x"}, run_id=task.current_run_id)
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    context = {"db": db, "task_id": tid, "run_id": task.current_run_id, "claim_lock": task.claim_lock}
    return tid, {"policy": {"kanban_json": json.dumps(context)}}


def _start_fails(frame, exc):
    """The real start path: ``_construct_agent`` books the failure, then re-raises it."""
    from agent import managed_worker

    def factory(**_kw):
        raise exc
    with pytest.raises(type(exc)):
        managed_worker._construct_agent(frame, factory, SimpleNamespace())


def _runs(conn, tid):
    return conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id", (tid,)).fetchall()


def _notices(conn):
    return conn.execute("SELECT id, body, idempotency_key, status FROM tasks WHERE idempotency_key LIKE ? "
                        "ORDER BY rowid", (NOTICE_PREFIX + "%",)).fetchall()


def _notice_comments(conn, notice_id):
    return conn.execute("SELECT body FROM task_comments WHERE task_id=?", (notice_id,)).fetchall()


# ---------------------------------------------------------------------------
# Host refusal: neutral.
# ---------------------------------------------------------------------------


def test_repeated_empty_registry_refusals_after_admission_leave_the_card_ready_uncounted(home):
    with kbc.connect() as conn:
        tid = None
        for submitter in (71001, 71002, 71003):
            tid, frame = _admitted_run(conn, tid, submitter=submitter)
            _start_fails(frame, _pin_error(EMPTY_REGISTRY))
            kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        runs = _runs(conn, tid)
        assert [r["outcome"] for r in runs] == ["owner_unavailable"] * 3
        assert "routing registry" in runs[-1]["error"] and "no turn ran" in runs[-1]["error"]


def test_host_refusals_still_reach_the_neutral_streak_breaker(home):
    with kbc.connect() as conn:
        tid = None
        for i in range(ou.NEUTRAL_STREAK_LIMIT + 1):
            tid, frame = _admitted_run(conn, tid, submitter=72000 + i)
            _start_fails(frame, _pin_error(EMPTY_REGISTRY))
            kbd.detect_crashed_workers(conn)
        runs = _runs(conn, tid)
        assert [r["outcome"] for r in runs[:-1]] == ["owner_unavailable"] * ou.NEUTRAL_STREAK_LIMIT
        assert runs[-1]["outcome"] == "crashed"
        assert f"{ou.NEUTRAL_STREAK_LIMIT} runs in a row found no owner" in runs[-1]["error"]
        assert kb.get_task(conn, tid).consecutive_failures == 1


# ---------------------------------------------------------------------------
# Card faults: still counted.
# ---------------------------------------------------------------------------


def test_a_genuinely_invalid_pin_counts_and_two_hit_the_failure_limit(home):
    with kbc.connect() as conn:
        tid = None
        for submitter in (73001, 73002):
            tid, frame = _admitted_run(conn, tid, submitter=submitter)
            _start_fails(frame, _pin_error(POPULATED_REGISTRY))
            kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert [r["outcome"] for r in _runs(conn, tid)] == ["crashed", "crashed"]
        assert task.consecutive_failures == 2 and task.status == "triage"
        assert not _notices(conn)


def _invalid_registry_error(config):
    from agent.model_routing_managed import HostRoutingConfigError, managed_config
    with pytest.raises(HostRoutingConfigError) as info:
        managed_config(config)
    return info.value


INVALID_REGISTRIES = {
    "routes_not_a_mapping": _registry("not-a-mapping"),
    "model_routing_not_a_mapping": {"model_routing": ["not", "a", "mapping"]},
    "bad_route_entry": _registry({"deep": [{"provider": "anthropic"}]}),
}


@pytest.mark.parametrize("name", sorted(INVALID_REGISTRIES))
def test_an_unreadable_or_invalid_registry_at_start_is_a_host_fault(home, name):
    with kbc.connect() as conn:
        tid = None
        for submitter in (76001, 76002, 76003):
            tid, frame = _admitted_run(conn, tid, submitter=submitter)
            _start_fails(frame, _invalid_registry_error(INVALID_REGISTRIES[name]))
            kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        runs = _runs(conn, tid)
        assert [r["outcome"] for r in runs] == ["owner_unavailable"] * 3
        assert "could not be read or validated" in runs[-1]["error"] and "no turn ran" in runs[-1]["error"]
        (notice,) = _notices(conn)
        assert "could not be read or validated" in notice["body"]


def test_managed_config_types_only_host_registry_faults():
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_managed import HostRoutingConfigError, managed_config
    exc = _invalid_registry_error(_registry("not-a-mapping"))
    assert isinstance(exc, ModelRoutingError)  # every existing fail-closed caller still catches it
    assert managed_config(POPULATED_REGISTRY) is POPULATED_REGISTRY
    assert managed_config({"model_routing": {"enabled": False, "routes": "ignored when off"}}) is None
    assert not isinstance(_pin_error(POPULATED_REGISTRY), HostRoutingConfigError)


def test_an_untyped_routing_error_at_start_still_counts(home):
    # Only the typed host-registry errors are host faults; never a blanket pass for routing errors.
    from agent.model_routing import ModelRoutingError
    with kbc.connect() as conn:
        tid, frame = _admitted_run(conn)
        _start_fails(frame, ModelRoutingError("Class selectors require provider auto; use a concrete model"))
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert not _notices(conn)


def test_an_invalid_registry_inside_a_turn_counts(home, monkeypatch):
    from gateway import session_kanban
    exc = _invalid_registry_error(_registry("not-a-mapping"))

    def turns(*_a):
        raise exc
    monkeypatch.setattr(session_kanban, "_run_task_turns", turns)
    with kbc.connect() as conn:
        tid, frame = _admitted_run(conn)
        with pytest.raises(type(exc)):
            session_kanban._run_worker_turns(SimpleNamespace(), frame, [])
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_an_empty_registry_refusal_inside_a_turn_counts(home, monkeypatch):
    from gateway import session_kanban
    exc = _pin_error(EMPTY_REGISTRY)

    def turns(*_a):
        raise exc
    monkeypatch.setattr(session_kanban, "_run_task_turns", turns)
    with kbc.connect() as conn:
        tid, frame = _admitted_run(conn)
        with pytest.raises(type(exc)):
            session_kanban._run_worker_turns(SimpleNamespace(), frame, [])
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_an_empty_registry_refusal_after_the_turn_counts(home):
    from gateway import session_kanban
    with kbc.connect() as conn:
        tid, frame = _admitted_run(conn)
        session_kanban.record_worker_failure(frame, _pin_error(EMPTY_REGISTRY))
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_a_proof_not_written_by_the_bound_interpreter_counts(home):
    with kbc.connect() as conn:
        tid, _frame = _admitted_run(conn, submitter=74001)
        task = kb.get_task(conn, tid)
        with kb.write_txn(conn):  # the submitter's pid, not the bound interpreter's
            kb._append_event(conn, tid, "worker_result", {
                "pid": 74001, "claim_lock": task.claim_lock, "exit_code": ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE,
                "last_output": "", "host_refusal": {"kind": "managed_registry_empty", "phase": "startup"}},
                run_id=task.current_run_id)
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


# ---------------------------------------------------------------------------
# The host notice: once per window, not per card.
# ---------------------------------------------------------------------------


def test_host_notice_dedups_across_cards_and_reappears_next_window(home, monkeypatch):
    hr = importlib.import_module("hermes_cli.kanban_host_refusal")
    with kbc.connect() as conn:
        cards = []
        for submitter in (75001, 75002):
            tid, frame = _admitted_run(conn, submitter=submitter)
            _start_fails(frame, _pin_error(EMPTY_REGISTRY))
            cards.append(tid)
        kbd.detect_crashed_workers(conn)  # both refused cards in one sweep
        assert all(kb.get_task(conn, t).status == "ready" for t in cards)
        (notice,) = _notices(conn)
        assert cards[0] in notice["body"] or cards[1] in notice["body"]
        from hermes_cli.kanban_model_neutral import validate_text
        validate_text(notice["body"])
        assert "anthropic/claude-opus-5-5" in notice["body"]  # observed route, not a new restriction
        row = kb.get_task(conn, notice["id"])
        assert (row.model_override, row.provider_override, row.model_pin_mandatory) == (None, None, None)
        assert _notice_comments(conn, notice["id"]) == []

        tid, frame = _admitted_run(conn, submitter=75003)  # same window: still one notice, no comment
        _start_fails(frame, _pin_error(EMPTY_REGISTRY))
        kbd.detect_crashed_workers(conn)
        assert len(_notices(conn)) == 1 and _notice_comments(conn, notice["id"]) == []

        later = __import__("time").time() + hr.NOTICE_WINDOW_SECONDS
        monkeypatch.setattr(hr, "time", SimpleNamespace(time=lambda: later))
        window2 = []
        for submitter in (75004, 75005):  # next window: one more notice for two more cards
            tid, frame = _admitted_run(conn, submitter=submitter)
            _start_fails(frame, _pin_error(EMPTY_REGISTRY))
            kbd.detect_crashed_workers(conn)
            window2.append(tid)
        assert len(_notices(conn)) == 1
        (again,) = _notice_comments(conn, notice["id"])
        assert window2[0] in again["body"] and window2[1] not in again["body"]
        assert "routing registry" in again["body"]


def test_host_notice_opens_a_fresh_card_once_the_last_one_is_done(home):
    """A done notice card keeps its key (no key stealing): the next round's key opens a fresh card,
    the round machinery the systemic-crash fix cards use."""
    hr = importlib.import_module("hermes_cli.kanban_host_refusal")
    proof = {"kind": "managed_registry_empty", "phase": "startup"}
    with kbc.connect() as conn:
        first = hr.notify(conn, "t_a", proof)
        kb.complete_task(conn, first, result="registry restored")
        second = hr.notify(conn, "t_b", proof)
        assert second and second != first
        assert hr.notify(conn, "t_c", proof) == second
        keys = {r["id"]: r["idempotency_key"] for r in _notices(conn)}
        assert keys[second] == keys[first] + ":r2"


def test_host_notice_never_raises_into_the_sweep(home, monkeypatch):
    import sqlite3
    hr = importlib.import_module("hermes_cli.kanban_host_refusal")

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(kb, "create_task", broken)
    with kbc.connect() as conn:
        assert hr.notify(conn, "t_a", {"kind": "managed_registry_empty", "phase": "startup"}) is None
        assert not _notices(conn) and not conn.in_transaction


# ---------------------------------------------------------------------------
# Pre-admission: the owner refused the claim with storage_unavailable (346d92ff665's requeue).
# ---------------------------------------------------------------------------


def _fake_clock(monkeypatch):
    from hermes_cli import kanban_worker_client as wc
    clock = [0.0]
    monkeypatch.setattr(wc, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def _sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(wc.asyncio, "sleep", _sleep)


def _owner(reason=None):
    """An owner whose ``kanban.run`` answers *reason* every time; ``None``: one that never answers
    (a restart drain), the transient owner-unavailable that is no host fault."""
    import asyncio
    from contextlib import asynccontextmanager
    from hermes_cli import gateway_client

    @asynccontextmanager
    async def _connect(*_, **__):
        if reason is None:
            raise gateway_client.GatewayUnavailableError("Gateway draining: restart_wait", "draining", "restart_wait")

        class _Client:
            events = asyncio.Queue()

            async def rpc(self, method, **params):
                raise gateway_client.GatewayRPCError(reason, 5001, {"reason": reason})
        yield _Client()
    return _connect


def _submitted_run(conn, monkeypatch, tid=None, owner=None):
    """A claim whose submitter is this process (it books its own refusal as ``tasks.worker_pid``),
    submitted to *owner* through the real worker client; returns the card id and the exit code."""
    from hermes_cli import gateway_client
    from hermes_cli import kanban_worker_client as wc
    tid = tid or kb.create_task(conn, title="card", assignee="default")
    kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w{os.getpid()}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), tid))
    task = kb.get_task(conn, tid)
    monkeypatch.setattr(gateway_client, "connect_gateway", owner or _owner("storage_unavailable"))
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": tid,
                       "HERMES_KANBAN_RUN_ID": str(task.current_run_id), "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock,
                       "HERMES_KANBAN_DB": db}.items():
        monkeypatch.setenv(key, value)
    rc = wc._submit()
    # _submit drops every HERMES_KANBAN_* variable (owner startup must not inherit them), the crash
    # grace included; without it the sweep never reaps the dead run.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    return tid, rc


def test_preadmission_storage_refusals_file_one_host_notice_per_window(home, monkeypatch):
    hr = importlib.import_module("hermes_cli.kanban_host_refusal")
    _fake_clock(monkeypatch)
    with kbc.connect() as conn:
        cards = []
        for _ in range(2):  # two cards refused in one window: one notice card, no comment
            tid, rc = _submitted_run(conn, monkeypatch)
            assert rc == ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            kbd.detect_crashed_workers(conn)
            cards.append(tid)
        for tid in cards:
            task = kb.get_task(conn, tid)
            assert task.status == "ready" and task.consecutive_failures == 0
            (run,) = _runs(conn, tid)
            assert run["outcome"] == "owner_unavailable" and "storage_unavailable" in run["error"]
        (notice,) = _notices(conn)
        assert "storage_unavailable" in notice["body"] and cards[0] in notice["body"]
        assert _notice_comments(conn, notice["id"]) == []

        later = __import__("time").time() + hr.NOTICE_WINDOW_SECONDS
        monkeypatch.setattr(hr, "time", SimpleNamespace(time=lambda: later))
        window2 = []
        for _ in range(2):  # the next window: one comment for two more refused cards
            tid, _rc = _submitted_run(conn, monkeypatch)
            kbd.detect_crashed_workers(conn)
            window2.append(tid)
        assert len(_notices(conn)) == 1
        (again,) = _notice_comments(conn, notice["id"])
        assert window2[0] in again["body"] and window2[1] not in again["body"]
        assert "storage_unavailable" in again["body"]


def test_preadmission_capsule_refusal_books_its_proof_and_requeues(home, monkeypatch):
    # worker-3 2026-11-13 07:21-08:25Z (t_01a1d1197fa00599 runs 56160..56810): the owner refused
    # ``kanban.run`` with capsule_observation_missing before any session; the client booked a plain exit 1
    # with no proof, so the runs were crashed (triage) and no land gate could tell them from an agent turn.
    hr = importlib.import_module("hermes_cli.kanban_host_refusal")
    _fake_clock(monkeypatch)
    proof = {"kind": "owner_capsule_unavailable", "phase": "preadmission", "reason": "capsule_observation_missing"}
    with kbc.connect() as conn:
        tid, rc = _submitted_run(conn, monkeypatch, owner=_owner("capsule_observation_missing"))
        assert rc == ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
        (result,) = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result'", (tid,))]
        assert result["submitter"] is True and result["pid"] == os.getpid()
        assert result[hr.PROOF_KEY] == proof
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        (run,) = _runs(conn, tid)
        assert run["outcome"] == "owner_unavailable" and "capsule" in run["error"]
        (meta,) = conn.execute("SELECT metadata FROM task_runs WHERE task_id=?", (tid,)).fetchone()
        assert json.loads(meta)[hr.PROOF_KEY] == proof
        (notice,) = _notices(conn)
        assert "capsule_observation_missing" in notice["body"] and tid in notice["body"]


def test_an_admitted_capsule_refusal_gets_no_preadmission_proof():
    from hermes_cli import gateway_client
    from hermes_cli import kanban_worker_client as wc
    exc = gateway_client.GatewayRPCError("capsule_observation_missing", 5001, {})
    assert wc._host_refusal(exc, wc._Claim(answered=0.0))
    admitted = wc._Claim(answered=0.0)
    admitted.admission_id = "a1"
    assert not wc._host_refusal(exc, admitted)
    assert importlib.import_module("hermes_cli.kanban_host_refusal").preadmission_proof("capsule_unknown") is None


def test_startup_and_preadmission_host_faults_share_one_host_card(home, monkeypatch):
    _fake_clock(monkeypatch)
    with kbc.connect() as conn:
        _tid, frame = _admitted_run(conn, submitter=77001)
        _start_fails(frame, _pin_error(EMPTY_REGISTRY))
        kbd.detect_crashed_workers(conn)
        _submitted_run(conn, monkeypatch)
        kbd.detect_crashed_workers(conn)
        assert len(_notices(conn)) == 1


def test_a_silent_owner_requeues_without_a_host_notice(home, monkeypatch):
    # The restart drain the parent fix already requeued: transient, not a host fault to fix.
    _fake_clock(monkeypatch)
    with kbc.connect() as conn:
        tid, rc = _submitted_run(conn, monkeypatch, owner=_owner(None))
        assert rc == ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
        kbd.detect_crashed_workers(conn)
        (run,) = _runs(conn, tid)
        assert run["outcome"] == "owner_unavailable" and "never answered" in run["error"]
        assert not _notices(conn)


def test_a_card_level_owner_refusal_files_no_host_notice(home, monkeypatch):
    _fake_clock(monkeypatch)
    with kbc.connect() as conn:
        tid, rc = _submitted_run(conn, monkeypatch, owner=_owner("invalid_params"))
        assert rc == 1
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "crashed"
        assert not _notices(conn)


def _forged_preadmission_result(conn, tid, pid):
    task = kb.get_task(conn, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_result", {
            "pid": pid, "claim_lock": task.claim_lock, "exit_code": ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE,
            "last_output": "", "submitter": True,
            "host_refusal": {"kind": "owner_storage_unavailable", "phase": "preadmission"}},
            run_id=task.current_run_id)


def test_a_preadmission_proof_from_another_pid_files_no_notice(home, monkeypatch):
    from hermes_cli.kanban_db_dispatch_exit_code import exit_code_kind
    monkeypatch.setattr(kbd, "_classify_worker_exit",
                        lambda pid: exit_code_kind(ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default")
        kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w78001")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (78001, tid))
        _forged_preadmission_result(conn, tid, 78002)  # not the run's submitter (78001)
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "owner_unavailable"  # still the neutral requeue
        assert not _notices(conn)


def test_a_preadmission_proof_does_not_excuse_an_admitted_run(home):
    with kbc.connect() as conn:
        tid, _frame = _admitted_run(conn, submitter=os.getpid())
        _forged_preadmission_result(conn, tid, os.getpid())
        kbd.detect_crashed_workers(conn)
        assert _runs(conn, tid)[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert not _notices(conn)
