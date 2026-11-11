"""The updater negotiates the guarded instant-update checkpoint only for an explicit live intent.

The gateway side is real: a ``GatewayControlServer`` on a real Unix socket serving the actual
``gateway.update_checkpoint`` handlers, real admission flocks and the real cron reservation
registry. Only the runner (the gateway's session bookkeeping) is a stub.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import update_checkpoint as checkpoint
from gateway.control_socket import GatewayControlServer
from hermes_cli import admission_fence
from hermes_cli import update_cmd_posix_pause as pause
from hermes_cli import update_pause_record as record

pytestmark = pytest.mark.platforms("posix")

NONCE = "episode-one"


@pytest.fixture
def episode(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(checkpoint, "preserve_cron_slots", Mock())
    monkeypatch.setattr(checkpoint, "kanban_managed_count", lambda runner: runner.managed)
    runner = SimpleNamespace(
        config=object(), managed=1, active=1, cron=0,
        request_restart=Mock(return_value=True),
        _active_work_count=lambda: runner.active,
        _active_cron_job_count=lambda: runner.cron,
    )
    monkeypatch.setattr("gateway.run_active_work_verb._delegation_units", lambda now: [])
    return SimpleNamespace(home=home, runner=runner, held=[])


def write_intent(home, **over):
    now = time.time()
    intent = {"pid": os.getpid(), "kind": "unfenced", "checkpoint": True, "cron_cut": True,
              "created_at": now, "expires_at": now + 120, "nonce": NONCE, **over}
    (home / "core-update").mkdir(exist_ok=True)
    (home / "core-update/intent").write_text(json.dumps(intent))
    return intent


def hold(episode):
    admission_fence.set_hold(episode.home)


def reservation(episode, *, cron):
    """A real, flock-held reservation; ``cron`` registers it as a published cron run."""
    reservation = admission_fence.extend(episode.home)
    episode.held.append(reservation)
    if cron:
        from cron.scheduler import _running_job_ids, _running_lock, _running_reservations
        key = (str(episode.home), f"job-{len(episode.held)}")
        with _running_lock:
            _running_reservations[key] = reservation
            _running_job_ids.add(key)
    return reservation


@pytest.fixture(autouse=True)
def release(episode):
    yield
    from cron.scheduler import _running_job_ids, _running_lock, _running_reservations
    with _running_lock:
        for key in [k for k in _running_reservations if k[0] == str(episode.home)]:
            _running_reservations.pop(key)
            _running_job_ids.discard(key)
    for item in episode.held:
        item.close()


def ask(episode, *, checkpoint_verb=True, pid=None, entry_extra=None):
    """Run the real updater call against a real gateway control socket; return (result, entry, idle_calls)."""
    idle = []
    entry = {"pid": pid or os.getpid(), "home": str(episode.home), "kind": "systemd",
             "unit": "hermes-gateway-test.service", **(entry_extra or {})}

    async def scenario():
        loop = asyncio.get_running_loop()
        handlers = {"pause-if-idle-for-update": lambda: idle.append(1) or {"busy": True}}
        if checkpoint_verb:
            handlers["checkpoint-for-update"] = lambda params: checkpoint.checkpoint_for_update(
                episode.runner, loop, params)
        server = GatewayControlServer(episode.home, verb_handlers=handlers)
        assert await server.start()
        try:
            return await loop.run_in_executor(None, pause._ask_to_drain, entry)
        finally:
            await server.stop()

    return asyncio.run(scenario()), entry, idle


def test_explicit_intent_negotiates_the_checkpoint_for_known_managed_work(episode):
    write_intent(episode.home)
    hold(episode)
    result, entry, idle = ask(episode)
    assert result is True and idle == []
    assert entry["checkpoint_drain_timeout"] == checkpoint.CHECKPOINT_DRAIN_S == 70.0
    episode.runner.request_restart.assert_called_once_with(detached=False, via_service=True, update_intent=episode.home / "core-update/intent")


def test_exactly_known_cron_reservation_is_accepted(episode):
    write_intent(episode.home)
    reservation(episode, cron=True)
    hold(episode)
    episode.runner.active, episode.runner.cron = 2, 1
    result, entry, _ = ask(episode)
    assert result is True and entry["checkpoint_drain_timeout"] == 70.0


def test_unknown_reservation_or_work_is_refused_without_a_stop(episode):
    write_intent(episode.home)
    reservation(episode, cron=False)  # a held flock the gateway never published
    hold(episode)
    result, entry, idle = ask(episode)
    assert result is False and "checkpoint_drain_timeout" not in entry and idle == []
    episode.runner.request_restart.assert_not_called()


def test_unknown_active_work_is_refused(episode):
    write_intent(episode.home)
    hold(episode)
    episode.runner.active = 2  # one managed card + one unaccounted turn
    result, entry, _ = ask(episode)
    assert result is False and "checkpoint_drain_timeout" not in entry
    episode.runner.request_restart.assert_not_called()


@pytest.mark.parametrize("change", ["no-hold", "expired", "not-checkpoint", "wrong-kind", "dead-owner", "no-intent"])
def test_without_a_valid_explicit_intent_only_the_idle_verb_is_used(episode, change):
    if change != "no-intent":
        over = {"expired": {"expires_at": time.time() - 1}, "not-checkpoint": {"checkpoint": False},
                "wrong-kind": {"kind": "fenced"}, "dead-owner": {"pid": 2 ** 22 + 12345}}.get(change, {})
        write_intent(episode.home, **over)
    if change != "no-hold":
        hold(episode)
    result, entry, idle = ask(episode)
    assert result is False and idle == [1]  # the idle-only refusal answered; checkpoint never asked
    assert "checkpoint_drain_timeout" not in entry
    episode.runner.request_restart.assert_not_called()


def test_stale_nonce_is_refused_by_the_gateway_not_downgraded(episode, monkeypatch):
    write_intent(episode.home)
    hold(episode)
    monkeypatch.setattr(pause, "_checkpoint_nonce", lambda entry: "other-episode")
    result, entry, idle = ask(episode)
    assert result is False and idle == [] and "checkpoint_drain_timeout" not in entry
    episode.runner.request_restart.assert_not_called()


def test_gateway_without_the_checkpoint_verb_gets_only_the_stricter_idle_request(episode):
    write_intent(episode.home)
    hold(episode)
    result, entry, idle = ask(episode, checkpoint_verb=False)
    assert result is False and idle == [1]
    assert "checkpoint_drain_timeout" not in entry


@pytest.mark.parametrize("ack", [
    {"pausing": True, "checkpoint": True, "pid": 1, "drain_timeout": 70.0},          # another process
    {"pausing": True, "checkpoint": True, "pid": os.getpid(), "drain_timeout": 70.0, "nonce": "x"},
    {"pausing": True, "pid": os.getpid(), "drain_timeout": 70.0},                      # not a checkpoint
    {"pausing": True, "checkpoint": True, "pid": os.getpid()},                         # no bound
    {"pausing": True, "checkpoint": True, "pid": os.getpid(), "drain_timeout": True},
    {"pausing": True, "checkpoint": True, "pid": os.getpid(), "drain_timeout": -5},
])
def test_unbelievable_ack_never_extends_the_wait(episode, monkeypatch, ack):
    write_intent(episode.home)
    hold(episode)
    from gateway import control_socket
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: dict(ack))
    entry = {"pid": os.getpid(), "home": str(episode.home)}
    assert pause._ask_to_drain(entry) is None  # may be exiting: settled, never trusted
    assert "checkpoint_drain_timeout" not in entry


def test_echoed_nonce_is_accepted(episode, monkeypatch):
    write_intent(episode.home)
    hold(episode)
    from gateway import control_socket
    ack = {"pausing": True, "checkpoint": True, "pid": os.getpid(), "drain_timeout": 70.0, "nonce": NONCE}
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: dict(ack))
    entry = {"pid": os.getpid(), "home": str(episode.home)}
    assert pause._ask_to_drain(entry) is True and entry["checkpoint_drain_timeout"] == 70.0


# ---- the wait budget -------------------------------------------------------------------

def run_stop(monkeypatch, entry, drain):
    now = [0.0]
    monkeypatch.setattr(pause, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)))
    monkeypatch.setattr(pause, "_drain_budget", lambda: 1885.0)
    monkeypatch.setattr(pause, "_alive", lambda *a: True)
    monkeypatch.setattr(record, "mark_stop_sent", lambda *a: None)
    monkeypatch.setattr(pause, "_ask_to_drain", lambda e: e.update(drain) or True)
    stop = Mock()
    monkeypatch.setattr(pause, "_stop_at_once", stop)
    with pytest.raises(pause.PauseRefused, match="still draining"):
        pause._stop_gateways({}, [entry])
    stop.assert_not_called()  # never forced
    return now[0]


def entry():
    return {"pid": 4242, "kind": "systemd", "unit": "hermes-gateway-test.service"}


def test_default_budget_stays_thirty_seconds(monkeypatch):
    assert 30.0 <= run_stop(monkeypatch, entry(), {}) <= 31.0


def test_acknowledged_checkpoint_waits_seventy_seconds(monkeypatch):
    elapsed = run_stop(monkeypatch, entry(), {"checkpoint_drain_timeout": 70.0})
    assert 70.0 <= elapsed <= 71.0


def test_checkpoint_budget_is_capped_at_seventy(monkeypatch):
    assert run_stop(monkeypatch, entry(), {"checkpoint_drain_timeout": 5000.0}) <= 71.0


def test_short_checkpoint_budget_never_goes_below_default(monkeypatch):
    assert 30.0 <= run_stop(monkeypatch, entry(), {"checkpoint_drain_timeout": 10.0}) <= 31.0


@pytest.mark.parametrize("gone", [False, True])
def test_lost_checkpoint_ack_settles_seventy_seconds_without_authorizing_mutation(episode, monkeypatch, gone):
    write_intent(episode.home)
    hold(episode)
    from gateway import control_socket
    monkeypatch.setattr(control_socket, "query_gateway_control", Mock(side_effect=TimeoutError))
    item = {"pid": os.getpid(), "home": str(episode.home)}
    now = [0.0]
    monkeypatch.setattr(pause, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)))
    monkeypatch.setattr(pause, "_alive", lambda *a: not (gone and now[0] >= 40))
    monkeypatch.setattr(pause, "_drain_budget", lambda: 1885)
    monkeypatch.setattr(record, "mark_stop_sent", Mock())
    disarm = Mock()
    monkeypatch.setattr(pause, "_disarm", disarm)
    monkeypatch.setattr(pause, "_stop_at_once", Mock(side_effect=AssertionError("forced stop")))
    with pytest.raises(pause.PauseRefused):
        pause._stop_gateways({}, [item])
    assert item["checkpoint_settle_timeout"] == 70
    assert "checkpoint_drain_timeout" not in item
    if gone:
        assert 40 <= now[0] < 41
        disarm.assert_called_once_with(item)
    else:
        assert 70 <= now[0] < 71
        disarm.assert_not_called()

