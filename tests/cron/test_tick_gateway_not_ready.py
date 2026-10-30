"""A tick outside the gateway holds agent jobs while the gateway is not ready, on every tick path. [daveey]

hermes-hub 2026-10-29/30 (cron 6b4342ad83d9): a Hermes update restarted the gateway, the Desktop
``serve`` ticker took over the store inside the restart gap, dispatched an agent job, and
``connect_gateway`` answered ``Gateway draining: update_paused``. ``run_canonical_job`` booked that
as a FAILED run (failure_streak 3 over three updates). Only headless ticks had the "hold, fire next
tick" gate. Now every tick whose agent jobs would dial out (no owner bound in this process) goes
through the gate, and a refusal that races past the gate before admission is a held fire, not a
failed run.
"""
from datetime import timedelta
from contextlib import asynccontextmanager
import threading

import pytest

import cron.jobs as J
import cron.scheduler as S
from cron import scheduler_authority, scheduler_gateway_gate


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(S, "_hermes_home", home)
    monkeypatch.setattr(S, "_sweep_mcp_orphans", lambda: None)
    monkeypatch.setattr("gateway.session_cron.owner_for_home", lambda home: None)
    S._running_job_ids.clear()
    return home


def _due_agent_job(name="agent"):
    job = J.create_job(prompt="summarise inbox", schedule="every 10m", name=name, deliver="local")
    stored = J.load_jobs()
    slot = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    next(r for r in stored if r["id"] == job["id"])["next_run_at"] = slot
    J.save_jobs(stored)
    return job["id"], slot


def _stored(job_id):
    return next(j for j in J.load_jobs() if j["id"] == job_id)


def _gateway(monkeypatch, state, *, fenced=False, reason_code=None):
    """What discovery/the update fence report for this home (ensure reads the same seams)."""
    from hermes_cli import gateway_runtime
    from hermes_cli.gateway_runtime import GatewayDiscovery

    monkeypatch.setattr(gateway_runtime, "_update_fenced", lambda homes: fenced)
    monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint",
                        lambda home, timeout=2.0: GatewayDiscovery(state, reason_code=reason_code))


def _assert_held(job_id, slot):
    stored = _stored(job_id)
    assert stored["last_run_at"] is None and stored["last_status"] is None
    assert int(stored.get("failure_streak") or 0) == 0
    assert stored["next_run_at"] == slot, "the slot stays due: it fires on the first ready tick"
    assert stored.get("fire_claim") is None and "pending_slot" not in stored
    assert stored["last_fire_error"]["detail"]
    assert not (J.OUTPUT_DIR / job_id).exists() or not any((J.OUTPUT_DIR / job_id).iterdir()), \
        "no FAILED output document for a fire that never reached the gateway"


# --- (a) a non-headless tick (the Desktop serve ticker) during an update ---------------------------

def test_desktop_tick_during_update_holds_agent_job_instead_of_failing_it(cron_home, monkeypatch):
    """The live failure: gateway down for the update restart, update fence up. Before the fix the
    tick dispatched, ensure answered draining/update_paused and the run was booked FAILED."""
    _gateway(monkeypatch, "absent", fenced=True)
    job_id, slot = _due_agent_job()

    ran = S.tick(verbose=False)

    assert ran == 0
    _assert_held(job_id, slot)
    assert "update" in _stored(job_id)["last_fire_error"]["detail"]


def test_desktop_tick_holds_while_update_fence_stands_over_a_ready_gateway(cron_home, monkeypatch):
    """The fence covers the whole update, not just the restart gap: ensure answers update_paused
    for a still-running gateway too, so the tick holds then as well."""
    _gateway(monkeypatch, "ready", fenced=True)
    job_id, slot = _due_agent_job()

    assert S.tick(verbose=False) == 0
    _assert_held(job_id, slot)


@pytest.mark.parametrize("state,reason", [("starting", None), ("draining", "update_paused"),
                                          ("draining", "replaced")])
def test_desktop_tick_holds_while_gateway_starting_or_draining(cron_home, monkeypatch, state, reason):
    _gateway(monkeypatch, state, reason_code=reason)
    job_id, slot = _due_agent_job()

    assert S.tick(verbose=False) == 0
    _assert_held(job_id, slot)


def test_desktop_tick_may_still_start_an_absent_gateway(cron_home, monkeypatch):
    """Desktop is an interactive owner: with no gateway and no update it still dispatches (connect
    ensures one), unlike a headless crontab tick."""
    _gateway(monkeypatch, "absent")
    due = [{"id": "a", "prompt": "x"}, {"id": "s", "no_agent": True}]
    assert scheduler_gateway_gate.refuse_agent_jobs_without_gateway(due, may_start=True) == due
    assert scheduler_gateway_gate.refuse_agent_jobs_without_gateway(
        [dict(j) for j in due], may_start=False) == [due[1]]


# --- (b)/(c) the race between the gate's probe and the dispatch -----------------------------------

def _bypass_gate(monkeypatch):
    """The probe said ready; the update fence went up between the probe and connect."""
    monkeypatch.setattr(scheduler_gateway_gate, "refuse_agent_jobs_without_gateway",
                        lambda due, **_kw: due)


def _connect_raising(monkeypatch, exc):
    from hermes_cli import gateway_client

    @asynccontextmanager
    async def connect(*_a, **_k):
        raise exc
        yield

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)


def test_run_canonical_job_refused_before_admission_is_not_fired(cron_home, monkeypatch):
    from hermes_cli.gateway_client import GatewayUnavailableError

    _connect_raising(monkeypatch, GatewayUnavailableError(
        "Gateway draining: update_paused", "draining", "update_paused"))
    with pytest.raises(scheduler_authority.CronFireNotAdmitted):
        scheduler_authority.run_canonical_job({"id": "job"}, execution_id="fire")
    assert not list((cron_home / "cron" / "admissions").glob("*.json")), "nothing was submitted"


def test_update_refusal_racing_past_the_gate_keeps_job_due_and_streak_zero(cron_home, monkeypatch):
    from hermes_cli.gateway_client import GatewayUnavailableError

    _bypass_gate(monkeypatch)
    _connect_raising(monkeypatch, GatewayUnavailableError(
        "Gateway draining: update_paused", "draining", "update_paused"))
    job_id, slot = _due_agent_job()

    S.tick(verbose=False)

    _assert_held(job_id, slot)
    assert "update_paused" in _stored(job_id)["last_fire_error"]["detail"]
    from cron.executions import latest_execution
    row = latest_execution(job_id)
    assert row["status"] == "failed" and row["error"].startswith("Not admitted")


def test_definite_gateway_fault_before_admission_is_still_a_failure(cron_home, monkeypatch):
    """Only not-ready states are held; an owner that refuses for a real fault fails the run."""
    from hermes_cli.gateway_client import GatewayUnavailableError

    _bypass_gate(monkeypatch)
    _connect_raising(monkeypatch, GatewayUnavailableError(
        "Gateway inaccessible: control_owner", "inaccessible", "control_owner"))
    job_id, _slot = _due_agent_job()

    S.tick(verbose=False)

    stored = _stored(job_id)
    assert stored["last_status"] == "error" and stored["failure_streak"] == 1


def test_refusal_after_admission_still_increments_the_streak(cron_home, monkeypatch):
    from hermes_cli import gateway_client

    class Peer:
        async def rpc(self, method, **params):
            if method == "cron.submit":
                raise gateway_client.GatewayRPCError("owner refused", None, {})
            assert method == "cron.recover"
            return {"status": "missing"}

    @asynccontextmanager
    async def connect(*_a, **_k):
        yield Peer()

    _bypass_gate(monkeypatch)
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    job_id, _slot = _due_agent_job()

    S.tick(verbose=False)

    stored = _stored(job_id)
    assert stored["last_status"] == "error" and stored["failure_streak"] == 1


# --- detection guard: every ticker entry point goes through the gate ------------------------------

@pytest.fixture
def gate_calls(cron_home, monkeypatch):
    calls = []

    def record(due, *, may_start=False):
        calls.append(may_start)
        return [j for j in due if j.get("no_agent")]  # hold everything: nothing dispatches

    monkeypatch.setattr(scheduler_gateway_gate, "refuse_agent_jobs_without_gateway", record)
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", lambda **_k: None)
    _due_agent_job()
    return calls


def _one_cycle(monkeypatch):
    stop = threading.Event()
    real = J.record_ticker_heartbeat

    def beat(*a, **k):
        real(*a, **k)
        if k.get("success") is not None or a:
            stop.set()  # the loop's per-iteration heartbeat: one tick cycle has run

    monkeypatch.setattr(J, "record_ticker_heartbeat", beat)
    return stop


def test_guard_hermes_cron_tick_is_gated_headless(gate_calls):
    from hermes_cli.cron import cron_tick

    cron_tick()
    assert gate_calls == [False]


def test_guard_single_store_ticker_without_owner_is_gated(gate_calls, monkeypatch):
    from cron.scheduler_provider import InProcessCronScheduler

    InProcessCronScheduler().start(_one_cycle(monkeypatch), interval=0)
    assert gate_calls == [True]


def test_guard_desktop_ticker_is_gated(gate_calls, cron_home, monkeypatch):
    """The real ``_start_desktop_cron_ticker`` path (multiplex ticker over served profiles)."""
    from hermes_cli import profiles, web_server

    monkeypatch.setattr(profiles, "profiles_to_serve", lambda **_k: [("default", cron_home)])
    monkeypatch.setattr(web_server, "_gateway_owns_cron", lambda name, home: False)
    monkeypatch.setattr("hermes_logging.enable_profile_log_routing", lambda homes: None)

    web_server._start_desktop_cron_ticker(_one_cycle(monkeypatch), interval=0)
    assert gate_calls == [True]


def test_guard_gateway_hosted_tick_skips_the_gate(gate_calls, cron_home, monkeypatch):
    """Inside the gateway the job is admitted in-process (owner bound): no probe, no hold."""
    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setattr("gateway.session_cron.owner_for_home", lambda home: (object(), object()))
    monkeypatch.setattr(S, "_submit_with_guard", lambda job, pool, fn: None)

    InProcessCronScheduler().start(_one_cycle(monkeypatch), interval=0)
    assert gate_calls == []
