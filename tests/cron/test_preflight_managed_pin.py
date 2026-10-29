"""Cron preflight refuses a pin the managed routing registry rejects. [daveey]

On the mini (``model_routing.enforce_subscription_only: true``) the ``imsg-spam-classifier`` job
was pinned to ``claude-sonnet-5``, which the registry no longer lists (only ``claude-sonnet-5-5``).
Every woken run died before the model with "Managed subscription policy rejects unregistered or
ambiguous concrete pin": the provider-key preflight swallowed the ManagedPinRejectedError as a
non-auth error and failed open, so the job errored on every wake instead of showing
``blocked_config`` once. Entry path: ``run_job`` against a temp config.yaml (the
test_preflight_config harness), plus ``run_one_job`` for the alert-once bookkeeping.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as cron_jobs
import cron.scheduler as sched
from tests.cron.test_preflight_config import _job, _run_job_patched

# Upstream admits agent cron runs through the canonical gateway owner (cdbe8c35adf); run_job
# here goes through the production owner execution seam, never a spawned gateway.
pytestmark = pytest.mark.usefixtures("cron_owner")


def managed_yaml(enforce=True):
    lines = ["model_routing:", "  enabled: true", "  policy_version: 1", "  allow_metered: false",
             f"  enforce_subscription_only: {'true' if enforce else 'false'}", "  routes:"]
    for level, provider, model in (("deep", "anthropic", "claude-opus-5-5"),
                                   ("standard", "anthropic", "claude-sonnet-5-5"),
                                   ("standard", "openai-codex", "gpt-6.1-sol")):
        if f"    {level}:" not in lines:
            lines.append(f"    {level}:")
        lines += [f"      - provider: {provider}", f"        model: {model}", "        billing: subscription",
                  "        weight: 1", "        eligible: true", "        funding_verified: true",
                  "        capabilities: {tools: true, streaming: true}"]
    return "\n".join(lines) + "\n"


def _write_config(home, enforce=True):
    (home / "config.yaml").write_text(
        "model:\n  default: claude-opus-5-5\n  provider: anthropic\n" + managed_yaml(enforce), encoding="utf-8")


def _run(tmp_path, **job_overrides):
    job = _job(**job_overrides)
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        return _run_job_patched(job, tmp_path)


@pytest.mark.parametrize("provider", [None, "anthropic"])
def test_unregistered_pin_is_blocked_config_naming_allowed_pins(tmp_path, provider):
    _write_config(tmp_path)
    success, output, _final, error, agent_constructed = _run(
        tmp_path, model="claude-sonnet-5", provider=provider)
    assert agent_constructed is False
    assert success is False
    assert "[blocked_config]" in (error or "")
    assert "'claude-sonnet-5'" in error
    # Names what the job CAN pin, so the alert is actionable.
    assert "claude-sonnet-5-5" in error and "claude-opus-5-5" in error
    assert "hermes cron edit pf-test --model" in error
    assert "BLOCKED" in output


def test_registered_pin_runs(tmp_path):
    _write_config(tmp_path)
    success, _output, _final, error, agent_constructed = _run(
        tmp_path, model="claude-sonnet-5-5", provider="anthropic")
    assert "[blocked_config]" not in (error or "")
    assert agent_constructed is True
    assert success is True, error


def test_policy_off_does_not_block(tmp_path):
    _write_config(tmp_path, enforce=False)
    success, _output, _final, error, agent_constructed = _run(tmp_path, model="claude-sonnet-5")
    assert "[blocked_config]" not in (error or "")
    assert agent_constructed is True
    assert success is True, error


def test_unpinned_job_on_rejected_cron_model_is_blocked(tmp_path):
    """The fleet default ``cron.model`` is a stored pin too."""
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: claude-opus-5-5\n  provider: anthropic\ncron:\n  model: claude-sonnet-5\n"
        + managed_yaml(), encoding="utf-8")
    # Upstream's shared _job now pins "test-model" by default; this case is the unpinned job.
    success, _output, _final, error, agent_constructed = _run(tmp_path, model=None)
    assert agent_constructed is False and success is False
    assert "[blocked_config]" in (error or "") and "cron.model" in error


def test_rejected_pin_alerts_once_across_woken_runs(tmp_path):
    """Two woken runs of the stale-pin job deliver ONE alert and persist blocked_config."""
    _write_config(tmp_path)
    # Upstream delivers owner-run results through the durable delivery queue (a "local" lane
    # enqueues nothing), so alert to a real target and count enqueues, as test_preflight_config does.
    job = _job(model="claude-sonnet-5", failure_deliver="slack:alerts")
    deliveries = []

    def fake_deliver(execution_id, job, content, **kwargs):
        deliveries.append(content)
        return {"status": "pending"}

    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        for _tick in range(2):
            fresh = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])
            with patch("cron.scheduler._hermes_home", tmp_path), \
                 patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state_registry.acquire", return_value=MagicMock()), \
                 patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
                 patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                       return_value={"api_key": "k", "base_url": "https://example.invalid/v1",
                                     "provider": "anthropic", "api_mode": "chat_completions"}), \
                 patch("cron.delivery_queue.enqueue", side_effect=fake_deliver), \
                 patch("run_agent.AIAgent") as mock_agent_cls:
                assert sched.run_one_job(fresh) is True
                assert mock_agent_cls.called is False
        stored = next(j for j in cron_jobs.load_jobs() if j["id"] == job["id"])

    assert stored["last_status"] == "blocked_config"
    assert len(deliveries) == 1, deliveries
    assert "claude-sonnet-5-5" in deliveries[0]
