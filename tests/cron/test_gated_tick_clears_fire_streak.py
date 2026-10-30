"""A gate tick heals a failure streak made only of fire/infra refusals. [daveey]

hermes-hub cron 6b4342ad83d9 (a monitor whose input never changes): three fires failed before
they reached the agent (``GatewayUnavailableError`` while the gateway restarted for an update),
leaving ``failure_streak`` 3. Every later tick was an unchanged-monitor gate tick, which keeps the
last verdict, so the streak stayed >= threshold forever and coach self_heal never saw the job fixed.

Now a failure that never reached the agent is booked ``last_error_kind = "fire"``. A gate tick
clears a streak made only of those; any agent failure in the streak keeps today's keep-verdict
behaviour. Entry path: ``S.tick`` against an owner reached through ``connect_gateway``, which
answers a gate tick with ``job_flags = {"_agent_skipped": True}`` (as ``gateway.session_cron`` does).
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import itertools

import pytest

import cron.jobs as J
import cron.scheduler as S
from tests.cron.test_tick_gateway_not_ready import (
    _bypass_gate, _connect_raising, _due_agent_job, _stored, cron_home as _cron_home,
)

# Every test runs in the not-ready suite's isolated cron home (no owner bound in this process);
# the fixture is imported under a module-local name so no test parameter shadows the import.
pytestmark = pytest.mark.usefixtures("_cron_home")
assert _cron_home  # pytest registers the imported fixture under this module-local name


_slots = itertools.count(2)


def _make_due(job_id):
    """Allocate distinct past occurrences independently of elapsed wall time."""
    stored = J.load_jobs()
    rec = next(r for r in stored if r["id"] == job_id)
    # A moving `now - counter` collides when both advance by one second.
    anchor = datetime.fromisoformat(rec["created_at"]).replace(microsecond=0)
    slot = anchor - timedelta(minutes=1, seconds=next(_slots))
    rec["next_run_at"] = slot.isoformat()
    J.save_jobs(stored)


def _owner(monkeypatch, result, job_flags=None, *, refuse_submit=False):
    """An owner gateway that runs the fire and reports *result* (or refuses it before admission)."""
    from hermes_cli import gateway_client

    class Peer:
        async def rpc(self, method, **params):
            if method == "cron.submit":
                if refuse_submit:
                    raise gateway_client.GatewayRPCError("owner refused", None, {})
                return {"session_id": "s", "admission_id": "a"}
            if method == "cron.recover":
                return {"status": "missing"}
            assert method == "cron.status"
            return {"status": "terminal", "result": list(result), "job_flags": job_flags or {}}

    @asynccontextmanager
    async def connect(*_a, **_k):
        yield Peer()

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)


def _fire_fault(monkeypatch):
    from hermes_cli.gateway_client import GatewayUnavailableError
    _connect_raising(monkeypatch, GatewayUnavailableError(
        "Gateway inaccessible: control_owner", "inaccessible", "control_owner"))


def _gate_tick(monkeypatch, job_id):
    _owner(monkeypatch, (True, "# no_change (agent run suppressed)\n", "[SILENT]", None),
           {"_agent_skipped": True})
    _make_due(job_id)
    S.tick(verbose=False)


def _agent_failure(monkeypatch, job_id):
    _owner(monkeypatch, (False, "# FAILED\n", "", "RuntimeError: model said no"))
    _make_due(job_id)
    S.tick(verbose=False)


def test_fire_failures_before_admission_are_booked_as_fire(monkeypatch):
    _bypass_gate(monkeypatch)
    _fire_fault(monkeypatch)
    job_id, _slot = _due_agent_job()
    for n in (1, 2, 3):
        if n > 1:
            _make_due(job_id)
        S.tick(verbose=False)
        stored = _stored(job_id)
        assert stored["failure_streak"] == n and stored["last_error_kind"] == "fire"


def test_owner_refusal_before_admission_is_booked_as_fire(monkeypatch):
    _bypass_gate(monkeypatch)
    _owner(monkeypatch, None, refuse_submit=True)
    job_id, _slot = _due_agent_job()
    S.tick(verbose=False)
    stored = _stored(job_id)
    assert stored["last_status"] == "error" and stored["failure_streak"] == 1
    assert stored["last_error_kind"] == "fire"


def test_gate_tick_clears_a_streak_made_only_of_fire_failures(monkeypatch):
    """The live hub case: three infra refusals, then unchanged-monitor ticks only."""
    _bypass_gate(monkeypatch)
    _fire_fault(monkeypatch)
    job_id, _slot = _due_agent_job()
    for n in range(3):
        if n:
            _make_due(job_id)
        S.tick(verbose=False)
    J.mark_preflight_alerted(job_id)
    assert _stored(job_id)["failure_streak"] == 3

    _gate_tick(monkeypatch, job_id)

    healed = _stored(job_id)
    assert healed["failure_streak"] == 0
    assert healed["last_status"] == "ok" and healed["last_error"] is None
    assert "last_error_kind" not in healed
    assert not healed.get("preflight_alerted"), "a future break must alert again"
    assert healed["last_failure"]["detail"], "the sticky last-failure stamp survives"


def test_gate_tick_keeps_a_streak_with_a_genuine_agent_failure(monkeypatch):
    """An agent verdict in the streak is kept by a gate tick (today's behaviour), even when an
    infra refusal came after it."""
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job()
    _agent_failure(monkeypatch, job_id)
    assert _stored(job_id)["last_error_kind"] == "agent"
    _fire_fault(monkeypatch)
    _make_due(job_id)
    S.tick(verbose=False)
    stored = _stored(job_id)
    assert stored["failure_streak"] == 2 and stored["last_error_kind"] == "agent"

    _gate_tick(monkeypatch, job_id)

    kept = _stored(job_id)
    assert kept["failure_streak"] == 2 and kept["last_status"] == "error"
    assert kept["last_error"] == stored["last_error"]
    assert kept["last_error_kind"] == "agent"


def test_gate_tick_keeps_an_agent_only_streak(monkeypatch):
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job()
    _agent_failure(monkeypatch, job_id)

    _gate_tick(monkeypatch, job_id)

    kept = _stored(job_id)
    assert kept["failure_streak"] == 1 and kept["last_status"] == "error"
    assert kept["last_error"] == "RuntimeError: model said no"


@pytest.mark.parametrize("elapsed_seconds", [0, 1, 2])
def test_real_success_resets_the_kind(monkeypatch, elapsed_seconds):
    now = J._hermes_now()
    monkeypatch.setattr(J, "_hermes_now", lambda: now)
    _bypass_gate(monkeypatch)
    job_id, _slot = _due_agent_job()
    _agent_failure(monkeypatch, job_id)
    _owner(monkeypatch, (True, "# ok\n", "done", None))
    _make_due(job_id)
    S.tick(verbose=False)
    healed = _stored(job_id)
    assert healed["failure_streak"] == 0 and "last_error_kind" not in healed
    # A scheduling gap must not reuse the successful occurrence's slot.
    now += timedelta(seconds=elapsed_seconds)
    # The next fire refusal starts a fresh fire-only streak.
    _fire_fault(monkeypatch)
    _make_due(job_id)
    S.tick(verbose=False)
    assert _stored(job_id)["last_error_kind"] == "fire"
