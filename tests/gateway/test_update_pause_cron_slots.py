"""The real pause verb records profile-local cron eligibility before requesting a drain."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


@pytest.mark.platforms("posix")
def test_pause_socket_records_cron_pause_before_drain(tmp_path, monkeypatch):
    from cron import jobs
    from gateway import run
    from gateway.control_socket import GatewayControlServer, pause_gateway_for_update
    from gateway.cron_pause import pause_for_update
    from hermes_constants import get_hermes_home

    now = datetime.now(UTC).replace(microsecond=0)
    clock = [now]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    job = jobs.create_job(prompt="due mid update", schedule=(now + timedelta(seconds=70)).isoformat())
    home = get_hermes_home()
    foreign, deleted = tmp_path / "foreign", tmp_path / "deleted"
    foreign.mkdir()
    monkeypatch.setattr(run, "_cron_tick_profile_homes", lambda _: [
        ("default", home), ("foreign", foreign), ("deleted", deleted)])
    monkeypatch.setattr(run, "_cron_profile_gate", lambda name, _: name != "foreign")
    requested = []

    def request_restart(**kwargs):
        assert (home / "cron" / "planned-pauses").exists()
        requested.append(kwargs)
        return True

    runner = SimpleNamespace(config=SimpleNamespace(), request_restart=request_restart)

    async def scenario():
        loop = asyncio.get_running_loop()
        server = GatewayControlServer(home=home, verb_handlers={
            "pause-for-update": lambda: pause_for_update(runner, loop)})
        assert await server.start()
        try:
            return await loop.run_in_executor(None, lambda: pause_gateway_for_update(home, timeout=10))
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    assert result and result["pausing"]
    assert requested == [{"detached": False, "via_service": True}]
    assert not (foreign / "cron").exists()
    assert not deleted.exists()
    clock[0] += timedelta(seconds=240)
    assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]
