"""A fire whose gateway socket never opened was not admitted: it is held, not booked FAILED. [daveey]

hermes-hub 2026-11-13 05:20 UTC (cron 6b4342ad83d9 and two siblings): a core-update drain released
31 deferred cron jobs at once, the gateway's event loop stalled ~4 s back to back, and the job's
worker could not open its WebSocket within ``connect_gateway``'s 10 s open timeout
(``Gateway connection failed; no local fallback``). Nothing was submitted and no admission journal
existed, yet ``run_canonical_job`` booked the fire FAILED. A dial failure before submit now holds
the fire (the slot stays due) for up to ``CONNECT_HOLD_LIMIT`` consecutive fires; the next one is
booked FAILED as before, so a gateway that stays unreachable still surfaces.
"""
from contextlib import asynccontextmanager
from datetime import timedelta

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
    # The gate's probe said ready; the dial then failed (the live race).
    monkeypatch.setattr(scheduler_gateway_gate, "refuse_agent_jobs_without_gateway", lambda due, **_kw: due)
    S._running_job_ids.clear()
    return home


def _due_agent_job():
    job = J.create_job(prompt="summarise inbox", schedule="every 10m", name="watcher", deliver="local")
    stored = J.load_jobs()
    slot = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    next(r for r in stored if r["id"] == job["id"])["next_run_at"] = slot
    J.save_jobs(stored)
    return job["id"], slot


def _stored(job_id):
    return next(j for j in J.load_jobs() if j["id"] == job_id)


def _outputs(job_id):
    out = J.OUTPUT_DIR / job_id
    return list(out.iterdir()) if out.exists() else []


def _journals(home):
    return list((home / "cron" / "admissions").glob("*.json"))


def _dial_failure():
    """The exact error ``connect_gateway`` raises when the WebSocket open times out."""
    from hermes_cli.gateway_client import GatewayClientError
    try:
        try:
            raise TimeoutError("timed out during opening handshake")
        except TimeoutError as cause:
            raise GatewayClientError("Gateway connection failed; no local fallback") from cause
    except GatewayClientError as exc:
        return exc


def _connect_raising(monkeypatch, make_exc):
    from hermes_cli import gateway_client
    dials = []

    @asynccontextmanager
    async def connect(*_a, **_k):
        dials.append(1)
        raise make_exc()
        yield

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    return dials


def _assert_held(home, job_id, slot):
    stored = _stored(job_id)
    assert stored["last_run_at"] is None and stored["last_status"] is None
    assert int(stored.get("failure_streak") or 0) == 0
    assert stored["next_run_at"] == slot, "the slot stays due: it fires on the next tick"
    assert stored.get("fire_claim") is None and "pending_slot" not in stored
    assert "connect_failed" in stored["last_fire_error"]["detail"]
    assert not _outputs(job_id), "no FAILED output document for a fire that never reached the gateway"
    assert not _journals(home), "nothing was submitted"


def test_dial_failure_before_submit_holds_the_fire(cron_home, monkeypatch):
    """The live failure: before the fix this tick booked the fire FAILED (streak 1, FAILED doc)."""
    dials = _connect_raising(monkeypatch, _dial_failure)
    job_id, slot = _due_agent_job()

    S.tick(verbose=False)

    assert dials, "the fire reached connect_gateway"
    _assert_held(cron_home, job_id, slot)


def test_run_canonical_job_raises_not_admitted_and_leaves_fire_error_unset(cron_home, monkeypatch):
    _connect_raising(monkeypatch, _dial_failure)
    job = {"id": "job"}
    with pytest.raises(scheduler_authority.CronFireNotAdmitted) as info:
        scheduler_authority.run_canonical_job(job, execution_id="fire")
    assert (info.value.state, info.value.reason_code) == ("unreachable", "connect_failed")
    assert "_fire_error" not in job
    assert not _journals(cron_home)


def test_client_error_without_a_transport_cause_still_fails(cron_home, monkeypatch):
    """Only a dial that failed at the socket is held; a local client refusal (bad explicit URL,
    protocol mismatch) is not a not-ready gateway and keeps failing loudly."""
    from hermes_cli.gateway_client import GatewayClientError
    _connect_raising(monkeypatch, lambda: GatewayClientError("Gateway protocol mismatch; update/restart required"))
    job_id, _slot = _due_agent_job()

    S.tick(verbose=False)

    stored = _stored(job_id)
    assert stored["last_status"] == "error" and stored["failure_streak"] == 1


def test_transport_loss_after_submit_stays_unknown_never_held(cron_home, monkeypatch):
    """Once a submit may have reached the owner, a lost socket is uncertainty, not a hold."""
    from cron import observer_reconnect
    from hermes_cli import gateway_client

    class Peer:
        async def rpc(self, method, **params):
            raise _dial_failure()

    @asynccontextmanager
    async def connect(*_a, **_k):
        yield Peer()

    monkeypatch.setattr(observer_reconnect, "RECONNECT_SECONDS", 0.0)
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    with pytest.raises(scheduler_authority.CronExecutionUnknown):
        scheduler_authority.run_canonical_job({"id": "job"}, execution_id="fire")
    assert _journals(cron_home), "the possibly-admitted identity is kept for reconciliation"


def test_dial_failure_past_the_hold_limit_is_booked_failed(cron_home, monkeypatch):
    """Detection guard: a gateway that stays unreachable is still one FAILED run and alert."""
    _connect_raising(monkeypatch, _dial_failure)
    job_id, slot = _due_agent_job()

    for _ in range(scheduler_authority.CONNECT_HOLD_LIMIT):
        S.tick(verbose=False)
        _assert_held(cron_home, job_id, slot)

    S.tick(verbose=False)

    stored = _stored(job_id)
    assert stored["last_status"] == "error" and stored["failure_streak"] == 1
    assert "Gateway connection failed" in stored["last_error"]
    assert _outputs(job_id), "the FAILED run writes its output document"


def test_a_successful_run_resets_the_hold_count(cron_home, monkeypatch):
    _connect_raising(monkeypatch, _dial_failure)
    job_id, slot = _due_agent_job()
    for _ in range(scheduler_authority.CONNECT_HOLD_LIMIT):
        S.tick(verbose=False)
    J.mark_job_run(job_id, True)
    stored = J.load_jobs()
    next(r for r in stored if r["id"] == job_id)["next_run_at"] = slot
    J.save_jobs(stored)

    S.tick(verbose=False)

    assert int(_stored(job_id).get("failure_streak") or 0) == 0
    assert _stored(job_id)["next_run_at"] == slot, "a fresh outage gets its own holds"
