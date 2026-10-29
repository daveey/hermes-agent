"""A tick whose gate suppressed the agent is not a healthy agent run. [daveey]

The mini's ``imsg-spam-classifier`` failed every woken run for a day, yet showed
``last_status=ok``: the silent once-a-minute ticks between wakes (wake-gate script printing
``{"wakeAgent": false}``, or an unchanged monitor) went through ``mark_job_run(success=True)``,
which reset ``failure_streak`` to 0 and cleared ``preflight_alerted``. So a 100% agent failure
read as "1 failure, recovered", repeatedly, and an alert-once blocked_config alert re-fired on
every wake. Entry path: ``run_one_job`` for both gates, with a real job store.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as cron_jobs
import cron.scheduler as sched
from cron.monitor import MonitorOutcome
from tests.cron.test_preflight_config import _job

# Upstream admits agent cron runs through the canonical gateway owner (cdbe8c35adf); run_job
# here goes through the production owner execution seam, never a spawned gateway.
pytestmark = pytest.mark.usefixtures("cron_owner")


def _tick(tmp_path, job_id, *, gate):
    fresh = next(j for j in cron_jobs.load_jobs() if j["id"] == job_id)
    extra = []
    if gate == "wake_gate":
        extra.append(patch.object(sched, "_run_job_script_with_claim_heartbeat",
                                  return_value=(True, '{"wakeAgent": false}')))
    else:
        extra.append(patch("cron.monitor.check_monitor", return_value=MonitorOutcome(ok=True, changed=False)))
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in [patch("cron.scheduler._hermes_home", tmp_path),
                  patch("cron.scheduler_delivery._resolve_origin", return_value=None),
                  patch("hermes_cli.env_loader.load_hermes_dotenv"),
                  patch("hermes_cli.env_loader.reset_secret_source_cache"),
                  patch("hermes_state_registry.acquire", return_value=MagicMock()),
                  patch.object(sched, "_deliver_result"), *extra]:
            stack.enter_context(p)
        agent_cls = stack.enter_context(patch("run_agent.AIAgent"))
        assert sched.run_one_job(fresh) is True
        assert agent_cls.called is False


@pytest.mark.parametrize("gate", ["wake_gate", "monitor_no_change"])
def test_silent_tick_keeps_failure_streak_and_alert_marker(tmp_path, gate):
    overrides = {"script": "gate.py"} if gate == "wake_gate" else {"monitor_script": "mon.py"}
    job = _job(**overrides)
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        assert cron_jobs.mark_job_run(job["id"], False, "[blocked_config] stale pin", status="blocked_config")
        cron_jobs.mark_preflight_alerted(job["id"])
        _tick(tmp_path, job["id"], gate=gate)
        stored = cron_jobs.get_job(job["id"])
        assert stored["failure_streak"] == 1
        assert stored["last_status"] == "blocked_config"
        assert stored["last_error"] == "[blocked_config] stale pin"
        assert stored.get("preflight_alerted")
        # The tick still counts as a fire for scheduling.
        assert stored["last_run_at"]
        # A real healthy run afterwards still resets everything.
        assert cron_jobs.mark_job_run(job["id"], True)
        healed = cron_jobs.get_job(job["id"])
        assert healed["failure_streak"] == 0 and healed["last_status"] == "ok"
        assert not healed.get("preflight_alerted")


def test_silent_tick_on_a_fresh_job_still_reads_ok(tmp_path):
    """No prior run: the gate tick records ok (nothing to preserve)."""
    job = _job(script="gate.py")
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        _tick(tmp_path, job["id"], gate="wake_gate")
        stored = cron_jobs.get_job(job["id"])
        assert stored["last_status"] == "ok"
        assert int(stored.get("failure_streak") or 0) == 0
