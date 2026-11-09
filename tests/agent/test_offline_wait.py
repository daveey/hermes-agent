"""A laptop losing DNS between model calls waits without spending retry attempts.

The regression includes a real local HTTP provider, wrapped DNS failures, and a clock whose
wall time advances across sleep while its awake clock barely moves.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.core.history._helpers import NO_BACKGROUND_REVIEW, OFFLINE_CONFIG
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, write_hermes_home

_OFFLINE_HOST = "llm.offline-test.invalid"
_DARKWAKE_GAIERROR = (8, "nodename nor servname provided, or not known")


class _FlakyDNS:
    """``socket.getaddrinfo`` for a host whose network is gone for the first ``offline_lookups``
    lookups (of any host this fake owns), then back. Real lookups pass through for other hosts."""

    def __init__(self, offline_lookups: int, owned=(_OFFLINE_HOST, "github.com"), after_ok: int = 0):
        self.remaining = offline_lookups
        self.after_ok = after_ok  # lookups that succeed BEFORE the outage starts (mid-run drop)
        self.owned = set(owned)
        self.lookups = 0
        self._real = socket.getaddrinfo

    def __call__(self, host, port, *args, **kwargs):
        if host not in self.owned:
            return self._real(host, port, *args, **kwargs)
        self.lookups += 1
        if self.after_ok > 0:
            self.after_ok -= 1
            return self._real("127.0.0.1", port, *args, **kwargs)
        if self.remaining > 0:
            self.remaining -= 1
            raise socket.gaierror(*_DARKWAKE_GAIERROR)
        return self._real("127.0.0.1", port, *args, **kwargs)


def _fast_offline_polls(monkeypatch):
    import agent.retry_utils as ru
    monkeypatch.setattr(ru, "OFFLINE_POLL_MIN_S", 0.01, raising=False)
    monkeypatch.setattr(ru, "OFFLINE_POLL_MAX_S", 0.02, raising=False)


def _turn(monkeypatch, dns: _FlakyDNS, *, offline_wait: int | None = None):
    from run_agent import AIAgent

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    home = Path(os.environ["HERMES_HOME"])
    extra = "  auto_recovery_cycles: 0\n"  # the old ladder must not be what rescues the turn
    if offline_wait is not None:
        extra += f"  offline_wait_seconds: {offline_wait}\n"
    with FakeLLMServer([Text("answered")]) as srv:
        base_url = srv.base_url.replace("127.0.0.1", _OFFLINE_HOST)
        # write_hermes_home's ``agent:`` block is last-but-telemetry; append our agent keys under it.
        write_hermes_home(home, base_url, extra_config=OFFLINE_CONFIG + NO_BACKGROUND_REVIEW)
        cfg = (home / "config.yaml").read_text()
        (home / "config.yaml").write_text(cfg.replace("agent:\n", "agent:\n" + extra, 1))
        agent = AIAgent(provider="custom", base_url=base_url, api_key="sk-fake", model="fake-model",
                        session_id="offline-wait", quiet_mode=True, platform="cli", skip_memory=True)
        try:
            result = agent.run_conversation("hi", conversation_history=[], task_id="t")
        finally:
            agent.close()
        served = len(srv.main_requests())
    return result, served


def test_turn_survives_dns_outage_without_spending_retries(monkeypatch):
    """gaierror until the network returns, then the SAME attempt goes through: the turn answers,
    and the provider saw exactly one request (no attempt was burned while offline)."""
    _fast_offline_polls(monkeypatch)
    import agent.turn_api_error as errors

    def no_rotation(*args, **kwargs):
        pytest.fail("local DNS outage must wait before credential recovery")

    monkeypatch.setattr(errors, "recover_after_classification", no_rotation)
    dns = _FlakyDNS(offline_lookups=40)
    result, served = _turn(monkeypatch, dns)
    assert result.get("completed") and result.get("final_response") == "answered", result
    assert served == 1
    assert dns.remaining == 0  # the outage really happened and was waited out


def test_offline_wait_disabled_keeps_the_old_failure(monkeypatch):
    """``agent.offline_wait_seconds: 0`` is the undo switch: the turn fails as before."""
    _fast_offline_polls(monkeypatch)
    result, served = _turn(monkeypatch, _FlakyDNS(offline_lookups=40), offline_wait=0)
    assert result.get("failed"), result
    assert served == 0


# ---------------------------------------------------------------------------------------------
# wait_for_network: awake-time budget, interrupts, misconfiguration


class _Clock:
    """Fake clocks for ``agent.retry_utils.time``: each sleep advances the monotonic (awake)
    clock by ``awake_per_sleep`` and the wall clock by ``wall_per_sleep`` (host sleep)."""

    def __init__(self, awake_per_sleep: float, wall_per_sleep: float):
        self.mono = 1000.0
        self.wall = 1_800_000_000.0
        self.awake_per_sleep, self.wall_per_sleep = awake_per_sleep, wall_per_sleep
        self.sleeps = 0

    def install(self, monkeypatch):
        import agent.retry_utils as ru
        fake = SimpleNamespace(**vars(time))
        fake.monotonic = lambda: self.mono
        fake.time = lambda: self.wall
        fake.sleep = self._sleep
        monkeypatch.setattr(ru, "time", fake)

    def _sleep(self, s):
        self.sleeps += 1
        self.mono += self.awake_per_sleep
        self.wall += self.wall_per_sleep


def _resolver(fail_first: int, control_ok: bool = False):
    calls = {"provider": 0, "control": 0}

    def resolve(host):
        if host == "github.com":
            calls["control"] += 1
            return control_ok
        calls["provider"] += 1
        return calls["provider"] > fail_first
    return resolve, calls


def test_wait_for_network_returns_online_after_four_failed_lookups(monkeypatch):
    from agent.retry_utils import wait_for_network
    _Clock(awake_per_sleep=0.2, wall_per_sleep=0.2).install(monkeypatch)
    resolve, calls = _resolver(fail_first=4)
    outcome, spent = wait_for_network("api.example.com", budget_s=900, resolve=resolve)
    assert outcome == "online"
    assert calls["provider"] == 5 and 0 < spent < 900


def test_wall_clock_jump_with_no_awake_time_does_not_exhaust(monkeypatch):
    """A laptop asleep for hours between polls: the wall clock jumps 10 min per poll slice but
    only 0.05 s of awake time passes. A 60 s budget must survive hundreds of such slices."""
    from agent.retry_utils import wait_for_network
    clock = _Clock(awake_per_sleep=0.05, wall_per_sleep=600.0)
    clock.install(monkeypatch)
    resolve, _ = _resolver(fail_first=3)
    outcome, spent = wait_for_network("api.example.com", budget_s=60, resolve=resolve)
    assert outcome == "online", (outcome, spent)
    assert clock.wall - 1_800_000_000.0 > 3600  # hours of wall time went by
    assert spent < 60


def test_wait_for_network_exhausts_on_awake_budget(monkeypatch):
    from agent.retry_utils import wait_for_network
    _Clock(awake_per_sleep=1.0, wall_per_sleep=1.0).install(monkeypatch)
    resolve, _ = _resolver(fail_first=10**9)
    outcome, spent = wait_for_network("api.example.com", budget_s=120, resolve=resolve)
    assert outcome == "exhausted" and 120 <= spent < 160


def test_wait_budget_caps_poll_and_counts_initial_dns(monkeypatch):
    from agent.retry_utils import wait_for_network
    clock = _Clock(awake_per_sleep=0.2, wall_per_sleep=0.2)
    clock.install(monkeypatch)

    def slow_dns(host):
        clock.mono += 0.4
        return False

    outcome, spent = wait_for_network("api.example.com", budget_s=1.0, resolve=slow_dns)
    assert outcome == "exhausted"
    assert 1.0 <= spent <= 1.01
    assert clock.mono - 1000 <= 1.01


def test_interrupt_before_dns_does_not_lookup(monkeypatch):
    from agent.retry_utils import wait_for_network

    def forbidden(host):
        pytest.fail("interrupted wait must not start DNS")

    assert wait_for_network("api.example.com", budget_s=900, resolve=forbidden,
                            is_interrupted=lambda: True) == ("interrupted", 0.0)


def test_wait_for_network_honours_interrupt(monkeypatch):
    from agent.retry_utils import wait_for_network
    clock = _Clock(awake_per_sleep=0.2, wall_per_sleep=0.2)
    clock.install(monkeypatch)
    resolve, _ = _resolver(fail_first=10**9)
    outcome, _spent = wait_for_network("api.example.com", budget_s=900, resolve=resolve,
                                       is_interrupted=lambda: clock.sleeps >= 3)
    assert outcome == "interrupted" and clock.sleeps <= 4


def test_provider_host_not_resolving_while_network_is_up_logs_error_and_does_not_wait(monkeypatch, caplog):
    from agent.retry_utils import wait_for_network
    clock = _Clock(awake_per_sleep=1.0, wall_per_sleep=1.0)
    clock.install(monkeypatch)
    resolve, _calls = _resolver(fail_first=10**9, control_ok=True)
    with caplog.at_level(logging.WARNING, logger="agent.retry_utils"):
        outcome, spent = wait_for_network("api.typo-example.com", budget_s=900, resolve=resolve)
    # A typo'd custom host is still reported, after at most MISCONFIG_RECHECKS re-checks of it.
    import agent.retry_utils as ru
    assert outcome == "misconfigured"
    assert _calls["provider"] == 1 + ru.MISCONFIG_RECHECKS
    assert spent <= ru.MISCONFIG_RECHECKS * ru.MISCONFIG_RECHECK_S + 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage().startswith("provider host does not resolve while network is up")


# A DNS blip on a waking/roaming laptop: the control name answers from cache while a fresh lookup of
# the provider host fails once (self-heal agent-e9b010909c: chatgpt.com reported "misconfigured" 55x
# while other sessions on the same Mac reached it seconds later).


@pytest.fixture(autouse=True)
def _fresh_resolved_hosts(monkeypatch):
    import agent.retry_utils as ru
    if hasattr(ru, "_RESOLVED_HOSTS"):
        monkeypatch.setattr(ru, "_RESOLVED_HOSTS", set())


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_builtin_provider_host_failing_once_then_resolving_is_online_not_misconfigured(monkeypatch, caplog):
    from agent.retry_utils import wait_for_network
    _Clock(awake_per_sleep=1.0, wall_per_sleep=1.0).install(monkeypatch)
    resolve, calls = _resolver(fail_first=1, control_ok=True)
    with caplog.at_level(logging.INFO, logger="agent.retry_utils"):
        outcome, _spent = wait_for_network("chatgpt.com", budget_s=900, resolve=resolve)
    assert outcome == "online"
    assert calls["provider"] == 2
    assert not _errors(caplog)


def test_custom_host_failing_once_then_resolving_is_online_not_misconfigured(monkeypatch, caplog):
    from agent.retry_utils import wait_for_network
    _Clock(awake_per_sleep=1.0, wall_per_sleep=1.0).install(monkeypatch)
    resolve, calls = _resolver(fail_first=1, control_ok=True)
    with caplog.at_level(logging.INFO, logger="agent.retry_utils"):
        outcome, _spent = wait_for_network("llm.custom-example.com", budget_s=900, resolve=resolve)
    assert outcome == "online" and calls["provider"] == 2
    assert not _errors(caplog)


def test_builtin_provider_host_that_keeps_failing_waits_without_error(monkeypatch, caplog):
    from agent.retry_utils import wait_for_network
    _Clock(awake_per_sleep=1.0, wall_per_sleep=1.0).install(monkeypatch)
    resolve, _calls = _resolver(fail_first=10**9, control_ok=True)
    with caplog.at_level(logging.WARNING, logger="agent.retry_utils"):
        outcome, spent = wait_for_network("chatgpt.com", budget_s=120, resolve=resolve)
    assert outcome == "exhausted" and spent >= 120
    assert not _errors(caplog)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "provider host stopped resolving" in warnings[0]


def test_host_that_resolved_earlier_in_this_process_waits_without_error(monkeypatch, caplog):
    from agent.retry_utils import wait_for_network
    _Clock(awake_per_sleep=1.0, wall_per_sleep=1.0).install(monkeypatch)
    host = "llm.custom-example.com"
    assert wait_for_network(host, budget_s=900, resolve=lambda h: True)[0] == "already_online"
    resolve, _calls = _resolver(fail_first=10**9, control_ok=True)
    with caplog.at_level(logging.WARNING, logger="agent.retry_utils"):
        outcome, _spent = wait_for_network(host, budget_s=120, resolve=resolve)
    assert outcome == "exhausted"
    assert not _errors(caplog)


def test_host_resolves_records_successful_lookups(monkeypatch):
    import agent.retry_utils as ru
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [("addr",)])
    assert ru.host_resolves("llm.custom-example.com")
    assert "llm.custom-example.com" in ru._RESOLVED_HOSTS


def test_turn_on_provider_dns_blip_with_network_up_logs_no_misconfiguration(monkeypatch, caplog):
    """Real turn: the provider host's first lookup fails while github.com resolves, then it resolves.
    The SAME attempt goes through and no "check the provider base_url" ERROR is filed."""
    _fast_offline_polls(monkeypatch)
    import agent.retry_utils as ru
    monkeypatch.setattr(ru, "MISCONFIG_RECHECK_S", 0.01, raising=False)

    class _Blip(_FlakyDNS):
        def __call__(self, host, port, *args, **kwargs):
            if host == "github.com":
                return self._real("127.0.0.1", port, *args, **kwargs)
            return super().__call__(host, port, *args, **kwargs)

    dns = _Blip(offline_lookups=2, owned=(_OFFLINE_HOST,))
    with caplog.at_level(logging.WARNING):
        result, served = _turn(monkeypatch, dns)
    assert result.get("completed") and result.get("final_response") == "answered", result
    assert served == 1 and dns.remaining == 0
    assert not [r for r in caplog.records
                if r.getMessage().startswith("provider host does not resolve while network is up")]


def test_both_hosts_failing_logs_one_offline_warning(monkeypatch, caplog):
    from agent.retry_utils import wait_for_network
    _Clock(awake_per_sleep=0.2, wall_per_sleep=0.2).install(monkeypatch)
    resolve, _ = _resolver(fail_first=3)
    with caplog.at_level(logging.WARNING, logger="agent.retry_utils"):
        outcome, _ = wait_for_network("api.example.com", budget_s=900, resolve=resolve)
    assert outcome == "online"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(msgs) == 1 and "offline — waiting for network" in msgs[0]


@pytest.mark.parametrize("make,expected", [
    (lambda: socket.gaierror(*_DARKWAKE_GAIERROR), True),
    (lambda: OSError(101, "Network is unreachable"), True),
    (lambda: ConnectionError("[Errno 8] nodename nor servname provided, or not known"), True),
    (lambda: TimeoutError("read timed out"), False),
    (lambda: ConnectionRefusedError(61, "Connection refused"), False),
])
def test_is_local_network_error_walks_the_cause_chain(make, expected):
    from agent.retry_utils import is_local_network_error
    inner = make()
    try:
        try:
            raise inner
        except Exception as e:
            raise RuntimeError("Connection error.") from e
    except RuntimeError as wrapped:
        assert is_local_network_error(wrapped) is expected
        assert is_local_network_error(inner) is expected
