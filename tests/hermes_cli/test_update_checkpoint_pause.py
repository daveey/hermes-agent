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



# ---- a refusal names its cause (hermes-hub 2026-11-12 13:53-13:56Z) ---------------------

def test_a_gateway_chat_turn_refusal_names_the_turn_through_the_real_socket(episode):
    """The hub's three refusals said only "busy or cannot acknowledge an idle pause": the gateway refused
    the checkpoint for a gateway-hosted chat turn (lane ``boundary-opus-review``, a ``oneshot``) and the
    updater threw the reason away. The real handler on a real socket must say which unit and session."""
    write_intent(episode.home)
    hold(episode)
    episode.runner.active = 2  # one managed card + one gateway chat turn
    episode.runner._running_agent_count = lambda: 1
    episode.runner._running_agents = {"agent:main:local:dm:local-be7f": object()}
    result, entry, idle = ask(episode)
    assert result is False and idle == []
    episode.runner.request_restart.assert_not_called()
    assert entry["refusal_reason"].startswith("gateway refused the update checkpoint: unpreservable work: ")
    assert "1 chat turn (agent:main:local:dm:local-be7f)" in entry["refusal_reason"]


@pytest.mark.parametrize("change, cause", [
    ("nonce", "nonce is not the live update intent's"),
    ("reservation", "admission reservation(s) not published by a cut cron run"),
])
def test_every_refusing_check_names_itself(episode, monkeypatch, change, cause):
    write_intent(episode.home)
    hold(episode)
    if change == "nonce":
        monkeypatch.setattr(pause, "_checkpoint_nonce", lambda entry: "other-episode")
    else:
        reservation(episode, cron=False)
    result, entry, _ = ask(episode)
    assert result is False and cause in entry["refusal_reason"]


def test_the_refusal_message_keeps_the_retry_token_and_adds_the_cause(monkeypatch):
    """hermes-sync retries only output containing "busy or cannot acknowledge an idle pause"; the cause
    rides along in brackets so the sync log names it."""
    monkeypatch.setattr(record, "mark_stop_sent", lambda *a: None)
    monkeypatch.setattr(record, "mark_stop_refused", lambda *a: None)
    def refuse(e):
        e["refusal_reason"] = "gateway refused the update checkpoint: unpreservable work: 1 chat turn (s1)"
        return False
    monkeypatch.setattr(pause, "_ask_to_drain", refuse)
    with pytest.raises(pause.PauseRefused) as raised:
        pause._stop_gateways({}, [entry()])
    text = str(raised.value)
    assert "busy or cannot acknowledge an idle pause [cause: gateway refused the update checkpoint: " \
           "unpreservable work: 1 chat turn (s1)]; retry when idle" in text


# ---- a duplicate `gateway run` is not a second gateway (12:29, 13:15, 13:55Z) -------------

def _bare_scan(monkeypatch, *, served, pids=(5555,)):
    from gateway import status
    from hermes_cli import gateway as gw, update_cmd_fleet, update_fleet_scope
    monkeypatch.setattr(pause, "_stop_busy_opted_in", lambda: False)
    monkeypatch.setattr(pause, "_supervisor_markers", lambda pid: None)
    monkeypatch.setattr(gw, "_get_service_pids", lambda **k: [4242])
    monkeypatch.setattr(gw, "find_gateway_pids", lambda **k: list(pids))
    monkeypatch.setattr(gw, "_capture_gateway_argv", lambda pid: ["hermes", "gateway", "run"])
    monkeypatch.setattr(update_cmd_fleet, "_scoped_manual_gateway_pids", lambda found: list(found))
    monkeypatch.setattr(update_fleet_scope, "gateway_pid_home", lambda pid: Path("/h/.hermes"))
    monkeypatch.setattr(status, "live_gateway_pid_for_home", lambda home: served)
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 1.0)
    return pause._discover_bare({4242})


def test_a_gateway_run_that_is_not_its_homes_gateway_is_not_paused(monkeypatch):
    """The hub update asked the real gateway (unit, PID 322771) to checkpoint, then asked a transient
    `gateway run` PID 487116 for the same home, which refused; the rollback restarted the drained real
    gateway on the old code. A PID that is not its home's verified gateway is not inventoried."""
    bare, notices = _bare_scan(monkeypatch, served=4242)
    assert bare == []
    assert notices == ["  ↷ gateway PID 5555 is not the gateway of /h/.hermes (PID 4242 serves it; "
                       "a duplicate start exits on its own): not paused"]


@pytest.mark.parametrize("served", [5555, None])
def test_a_bare_gateway_that_serves_its_home_or_is_unprovable_is_still_paused(monkeypatch, served):
    bare, notices = _bare_scan(monkeypatch, served=served)
    assert [b["pid"] for b in bare] == [5555] and notices == []
