"""A detached worker must protect its fire claim before advertising readiness."""
import json
import os
import socket

import pytest

from cron import executions, jobs, scheduler
from cron.jobs import use_cron_store


@pytest.mark.parametrize("claim_state", ["owned", "replaced", "removed"])
def test_worker_stamps_claim_before_ready_without_run_heartbeat(tmp_path, monkeypatch, claim_state):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr("hermes_cli.env_loader.hydrate_profile_secret_sources", lambda *_: None)
    monkeypatch.setattr("gateway.run_startup.GatewayStartupMixin._register_config_hooks", lambda *_a, **_k: None)

    # The gateway is provably dead; this process is the live adopting worker.
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == os.getpid())
    with use_cron_store(home):
        job = jobs.create_job(prompt="x", schedule="every 5m", name="adoption")
        assert jobs.claim_job_for_fire(job["id"])
        records = jobs.load_jobs()
        records[0]["fire_claim"]["by"] = f"{socket.gethostname()}:999999999:gateway"
        jobs.save_jobs(records)
        job = jobs.get_job(job["id"])
        job["execution_id"] = executions.create_execution(job["id"], source="cron")["id"]
        assert executions.mark_execution_handoff_pending(job["execution_id"])
        if claim_state == "replaced":
            records[0]["fire_claim"]["by"] = "replacement:123:token"
            jobs.save_jobs(records)
        elif claim_state == "removed":
            assert jobs.remove_job(job["id"])
            assert jobs.get_job(job["id"]) is None
    payload, ack = tmp_path / "payload.json", tmp_path / "ready"
    payload.write_text(json.dumps({"job": job, "profile_home": str(home)}))
    observed = []
    real_replace = os.replace

    def publish(src, dst):
        if dst == ack:
            observed.append("ready")
            if claim_state == "owned":
                claim = jobs.get_job(job["id"])["fire_claim"]
                assert claim.get("runner") == f"{socket.gethostname()}:{os.getpid()}"
                assert not jobs.claim_job_for_fire(job["id"])
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", publish)
    # No run_one_job heartbeat can rescue the claim before the ack assertion.
    monkeypatch.setattr(scheduler, "run_one_job", lambda *_a, **_k: observed.append("run") or True)

    assert scheduler._run_external_worker_payload(payload, ack) is True
    assert observed == (["ready", "run"] if claim_state == "owned" else ["ready"])
    assert ack.exists()
    with use_cron_store(home):
        record = executions.get_execution(job["execution_id"])
        assert record["status"] == {
            "owned": "running", "replaced": "failed", "removed": "cancelled"}[claim_state]
    if claim_state == "replaced":
        with use_cron_store(home):
            claim = jobs.get_job(job["id"])["fire_claim"]
            assert claim["by"] == "replacement:123:token"
            assert "runner" not in claim
