"""The restart-safe external worker keeps a monitor change pending until a successful agent run.

Each fire goes through the real ``_run_external_worker_payload`` -> ``run_one_job`` -> owner
``run_job`` path in an isolated home; only the agent, its provider and delivery are stubbed.
"""
import json
import time

import pytest

from cron import executions, jobs, scheduler
from cron.jobs import use_cron_store
from tests.cron.test_monitor_kind import _install_agent_stubs, _make_monitor_job, _write_script


@pytest.fixture
def worker_home(tmp_path, monkeypatch, cron_owner):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda *_: None)
    monkeypatch.setattr("gateway.run_startup.GatewayStartupMixin._register_config_hooks",
                        lambda *_a, **_k: None)
    return home


def _worker_fire(home, tmp_path, job_id, n):
    """One gateway-dispatched fire adopted by the external worker."""
    with use_cron_store(home):
        jobs.update_job(job_id, {"next_run_at": jobs._hermes_now().isoformat()})
        assert jobs.claim_job_for_fire(job_id)
        job = jobs.get_job(job_id)
        job["execution_id"] = executions.create_execution(job_id, source="cron")["id"]
        assert executions.mark_execution_handoff_pending(job["execution_id"])
    payload, ack = tmp_path / f"payload{n}.json", tmp_path / f"ready{n}"
    payload.write_text(json.dumps({"job": job, "profile_home": str(home)}))
    assert scheduler._run_external_worker_payload(payload, ack) is True
    assert ack.exists()
    with use_cron_store(home):
        return jobs.get_job(job_id)["monitor_state"]


def test_external_worker_quota_retry_then_delivery_failure_does_not_replay(
        worker_home, tmp_path, monkeypatch):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable

    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    with use_cron_store(worker_home):
        job = _make_monitor_job(worker_home, "echo 'state A'\n")
        assert scheduler.run_job(job)[0]  # baseline A acknowledged by a successful run
    _write_script(worker_home, "mon.sh", "echo 'state B'\n")

    construct = scheduler._construct_cron_agent

    def refused(*args, **kwargs):
        raise SubscriptionQuotaUnavailable("anthropic", "test", time.time() + 3600)

    monkeypatch.setattr(scheduler, "_construct_cron_agent", refused)
    state = _worker_fire(worker_home, tmp_path, job["id"], 1)
    assert observed["agent_runs"] == 1
    [pending] = state["pending_changes"]
    assert pending["old"] == "state A" and pending["new"] == "state B"

    # The agent now succeeds, but delivering its notice fails: that must not replay the agent.
    monkeypatch.setattr(scheduler, "_construct_cron_agent", construct)

    def delivery_down(d, *_a, **_k):
        d.should_deliver = d.delivery_attempted = True
        d.delivery_error = "delivery transport down"

    monkeypatch.setattr(scheduler, "_save_compose_deliver", delivery_down)
    state = _worker_fire(worker_home, tmp_path, job["id"], 2)
    assert observed["agent_runs"] == 2
    assert pending["id"] in observed["prompts"][-1]
    assert "-state A" in observed["prompts"][-1] and "+state B" in observed["prompts"][-1]
    assert state["pending_changes"] == []
    assert state["last_output"] == "state B"

    _worker_fire(worker_home, tmp_path, job["id"], 3)
    assert observed["agent_runs"] == 2, "an acknowledged change must stay suppressed"
