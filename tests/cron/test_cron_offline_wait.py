"""A cron run whose host loses DNS mid-run waits for the network and finishes ``ok``.

The first model call succeeds, its tool call takes DNS away, and the second model call must
wait it out. The unreachable rerun ladder cannot rescue a job that already made a model call.
"""
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home

_HOST = "llm.offline-test.invalid"

_TICK = '''
import json, os, socket, sys
from pathlib import Path

_real = socket.getaddrinfo
marker = Path(os.environ["OFFLINE_MARKER"])
state = {"remaining": int(os.environ["OFFLINE_LOOKUPS"])}

def _getaddrinfo(host, port, *a, **k):
    if host in ("llm.offline-test.invalid", "github.com"):
        if marker.exists() and state["remaining"] > 0:  # the job's tool call took the network away
            state["remaining"] -= 1
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return _real("127.0.0.1", port, *a, **k)
    return _real(host, port, *a, **k)

socket.getaddrinfo = _getaddrinfo
import agent.retry_utils as ru
ru.OFFLINE_POLL_MIN_S, ru.OFFLINE_POLL_MAX_S = 0.01, 0.02
from cron import scheduler, scheduler_authority
from tests.cron.conftest import owner_execution_runner
scheduler_authority.run_canonical_job = owner_execution_runner()
scheduler.tick(verbose=False, sync=True)
print(json.dumps({"remaining": state["remaining"], "marker": marker.exists()}))
'''


def _run_offline_job(tmp_path, agent_extra=""):
    from hermes_time import now

    home = tmp_path / "home"
    marker = home / "offline-now.txt"
    script = [ToolCall("write_file", {"path": str(marker), "content": "network gone\n"}), Text("done")]
    with FakeLLMServer(script) as srv:
        base_url = srv.base_url.replace("127.0.0.1", _HOST)
        write_hermes_home(home, base_url)
        cfg = (home / "config.yaml").read_text()
        (home / "config.yaml").write_text(cfg.replace("agent:\n", "agent:\n  auto_recovery_cycles: 0\n" + agent_extra, 1))
        (home / "cron").mkdir()
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
            "id": "offline", "name": "offline probe", "prompt": "write the note",
            "schedule": {"kind": "interval", "minutes": 240},
            "next_run_at": (now() - timedelta(minutes=1)).isoformat(),
            "enabled": True, "state": "scheduled", "deliver": "local",
            "repeat": {"times": None, "completed": 0}}]}))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HERMES_", "_HERMES_"))
               and not k.endswith(("_API_KEY", "_TOKEN"))}
        env.update(HERMES_HOME=str(home), PYTHONPATH=str(Path(__file__).resolve().parents[2]),
                   OFFLINE_MARKER=str(marker), OFFLINE_LOOKUPS="40")
        result = subprocess.run([sys.executable, "-c", _TICK], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=240, check=False)
        served = len(srv.main_requests())
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    job = json.loads((home / "cron" / "jobs.json").read_text())["jobs"][0]
    logs = "\n".join(p.read_text(errors="replace") for p in sorted((home / "logs").glob("*.log")))
    return report, job, served, result.stderr + logs


def test_cron_run_that_goes_offline_mid_run_still_finishes_ok(tmp_path):
    report, job, served, result = _run_offline_job(tmp_path)
    assert report == {"remaining": 0, "marker": True}, report  # the outage happened mid-run
    assert job.get("last_status") == "ok", (job, result[-3000:])
    assert served == 2  # tool-call turn + the answer after the wait; nothing burned offline


def test_offline_failure_keeps_the_provider_error_type(tmp_path):
    """With the wait off (``agent.offline_wait_seconds: 0``) the run still fails, but the failure is
    booked as the provider's ``APIConnectionError``, not a bare ``RuntimeError``: the coach detector
    skips transient connection errors by type and filed this one as a defect (cron-b459306dea)."""
    _report, job, _served, result = _run_offline_job(tmp_path, "  offline_wait_seconds: 0\n")
    assert job.get("last_status") == "error", job
    assert str(job.get("last_error") or "").startswith("APIConnectionError: Hermes can't reach the model provider"), job
    # The ERROR record the coach detector reads (it skips APIConnectionError, files RuntimeError).
    assert "Job 'offline probe' failed: APIConnectionError: " in result, result[-3000:]
    assert "Job 'offline probe' failed: RuntimeError" not in result
