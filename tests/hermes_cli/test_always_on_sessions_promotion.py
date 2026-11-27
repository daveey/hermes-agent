"""Native early-owner promotion precedes unrelated cold route work."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_cli import always_on_rollover as ro
from tests.hermes_cli import test_always_on_rollover as helpers

home = helpers.home

LIST = "/api/profiles/sessions?limit=50&offset=0&order=recent&profile=all&min_messages=1&archived=exclude"
SIDEBAR = "/api/profiles/sessions/sidebar?recents_profile=all"


def complete_list():
    return {"sessions": [{"id": "persisted-fixture"}], "total": 1, "profile_totals": {"default": 1},
            "limit": 50, "offset": 0, "errors": [], "storage": {}}


def complete_sidebar():
    return {**{key: {"sessions": [{"id": "persisted-fixture"}]} for key in ("recents", "cron", "messaging")},
            "errors": [], "storage": {}}


def native_roller(home):
    host = helpers.FakeHost(running={9148}, commits={9148: "a" * 40})
    ro.write_active(9148)
    roller = helpers.make_roller(home, host)
    original = host.get

    def get(port, path, timeout):
        if path == LIST:
            host.gets.append((port, path))
            return 200, 0, complete_list()
        if path == SIDEBAR:
            host.gets.append((port, path))
            return 200, 0, complete_sidebar()
        if path == "/api/host/identity":
            return 200, 0, {"ok": True, "pid": 1000 + port}
        if path == "/api/ssh/ownership":
            return 200, 0, {"ok": True, "sshOwnerNonce": "fixture-incarnation", "runtimeIntact": True}
        status, elapsed, body = original(port, path, timeout)
        if path == "/api/health" and body is not None:
            body["sessions_early_owner"] = True
        return status, elapsed, body

    roller.get = get
    return roller, host


def test_native_switches_only_after_both_selectors_before_any_broad_work(home):
    roller, host = native_roller(home)
    warm_seen = []

    def replay(pid):
        assert ro.read_active()["port"] == 9149
        assert (9149, LIST) in host.gets and (9149, SIDEBAR) in host.gets
        return []

    def boards(port, *, deadline):
        assert ro.read_active()["port"] == port
        return ["/api/plugins/kanban/board?board=fixture"]

    def warm(port, paths, *, deadline):
        warm_seen.append(ro.read_active())
        assert paths == ["/api/plugins/kanban/board?board=fixture"]
        return {"fixture": True}

    roller.replay_paths, roller.board_paths, roller.warm = replay, boards, warm
    assert roller.roll("fixture") is True
    assert len(warm_seen) == 1
    assert warm_seen[0] == ro.read_active()  # warm summary cannot overwrite publication
    assert ro.read_active()["warming"] == []
    assert not any(call[0] == "stop" for call in host.calls)


@pytest.mark.parametrize("state,expected", [
    ("pending", True), ("ready", True), ("failed", True), ("absent", False),
    ("not-adopted", False), ("closed", False), ("foreign", False), ("off", False),
])
def test_health_policy_is_actual_adopted_custody_not_capture_readiness(monkeypatch, state, expected):
    from hermes_cli.web_routers import status
    from hermes_cli.sessions_early_boot import EarlySessions
    runtime = SimpleNamespace(ready=state == "ready")
    owner = EarlySessions(runtime)
    if state != "not-adopted":
        owner.adopt()
    owner.closed = state == "closed"
    monkeypatch.setattr(status.app.state, "early_sessions_owner", None if state == "absent" else owner, raising=False)
    current = None if state == "off" else (SimpleNamespace() if state == "foreign" else runtime)
    monkeypatch.setattr(status.app.state, "sessions_facts", current, raising=False)
    monkeypatch.setenv("HERMES_FACTS_SESSIONS", "1")  # not authority for the policy
    assert asyncio.run(status.get_health())["sessions_early_owner"] is expected


@pytest.mark.parametrize("replacement", ["pid", "nonce", "auth", "host-pid"])
def test_native_proof_is_one_authenticated_process_incarnation(home, replacement):
    roller, _host = native_roller(home)
    original = roller.get
    ready = [False]

    def get(port, path, timeout):
        result = original(port, path, timeout)
        if path == SIDEBAR:
            ready[0] = True
        if ready[0] and path == "/api/host/identity" and replacement == "host-pid":
            return 200, 0, {"ok": True, "pid": 9999}
        if ready[0] and path == "/api/ssh/ownership":
            if replacement == "nonce":
                return 200, 0, {"ok": True, "sshOwnerNonce": "new-incarnation", "runtimeIntact": True}
            if replacement == "auth":
                return 401, 0, {"detail": "Unauthorized"}
        return result

    roller.get = get
    roller.unit_pid = lambda port, **kwargs: 9999 if ready[0] and replacement == "pid" else 1000 + port
    assert roller.roll("replacement") is False
    assert ro.read_active()["port"] == 9148
    assert not ro.read_active()["warming"]


@pytest.mark.parametrize("adopted", [False, True])
def test_health_failure_never_stops_first_boot_or_adopted_clients(home, adopted):
    host = helpers.FakeHost(running={9148, 9149} if adopted else set())
    if adopted:
        ro.write_active(9148, warming=[9149])
    roller = helpers.make_roller(home, host, boot_timeout_s=.2)
    roller.get = lambda port, path, timeout: (None, 0, None)
    assert roller.roll("unavailable") is False
    assert not any(call[0] == "stop" for call in host.calls)


def test_first_boot_failed_readiness_is_retried_not_permanently_healthy(home):
    roller, host = native_roller(home)
    host.running.clear()
    host.commits[9148] = "c" * 40  # no commit-drift guard may mask incomplete promotion
    ro.active_path().unlink()
    original = roller.get
    roller.get = lambda port, path, timeout: (503, 0, None) if path == LIST else original(port, path, timeout)
    assert roller.roll("first boot") is False
    assert not any(call[0] == "stop" for call in host.calls)
    assert roller.drift() is not None


def test_slow_legacy_health_still_gets_original_request_allowance(home):
    roller = helpers.make_roller(home, helpers.FakeHost())
    timeouts = []

    def slow(port, path, timeout):
        timeouts.append(timeout)
        roller.sleep(min(.4, timeout))
        return (200, .4, {"ok": True}) if timeout >= .4 else (None, timeout, None)

    roller.get = slow
    assert roller._await_health(9148) == {"ok": True}
    assert any(timeout >= .4 for timeout in timeouts)


@pytest.mark.parametrize("selector", [LIST, SIDEBAR])
@pytest.mark.parametrize("bad", ["auth", "unavailable", "malformed", "errors", "storage", "partial", "failed", "no-rows"])
def test_partial_or_unavailable_selector_never_promotes(home, selector, bad):
    roller, host = native_roller(home)
    original = roller.get

    def get(port, path, timeout):
        result = original(port, path, timeout)
        if path != selector:
            return result
        body = complete_list() if selector == LIST else complete_sidebar()
        if bad in ("auth", "unavailable"):
            return 401 if bad == "auth" else 503, 0, {"detail": "unavailable"}
        if bad == "malformed":
            return 200, 0, {}
        if bad == "errors":
            body["errors"] = [{"profile": "default", "error": "read failed"}]
        if bad == "storage":
            body["storage"] = {"default": "unavailable"}
        if bad == "partial":
            body["profiles_failed"] = {"default": {"failed": True}}
        if bad == "failed":
            body["failed"] = True
        if bad == "no-rows":
            if selector == LIST:
                body.pop("sessions")
            else:
                body["cron"].pop("sessions")
        return 200, 0, body

    roller.get = get
    assert roller.roll("bad selector") is False
    assert ro.read_active()["port"] == 9148
    assert not ro.read_active()["warming"]
    assert not any(path == "/api/plugins/kanban/boards" for _, path in host.gets)


def test_completed_empty_canonical_stores_are_not_an_unavailable_backend(home):
    roller, _host = native_roller(home)
    original = roller.get

    def get(port, path, timeout):
        if path == LIST:
            return 200, 0, {**complete_list(), "sessions": [], "total": 0, "profile_totals": {"default": 0}}
        if path == SIDEBAR:
            return 200, 0, {**{key: {"sessions": []} for key in ("recents", "cron", "messaging")},
                            "errors": [], "storage": {}}
        return original(port, path, timeout)

    roller.get = get
    assert roller.roll("legitimate empty") is True  # product policy, NOT real-row acceptance


def test_each_selector_waits_independently_without_reprobing_success(home):
    roller, _host = native_roller(home)
    original = roller.get
    calls = {LIST: 0, SIDEBAR: 0}

    def get(port, path, timeout):
        if path in calls:
            calls[path] += 1
            if path == SIDEBAR and calls[path] < 3:
                return 503, 0, {"detail": "facts unavailable"}
        return original(port, path, timeout)

    roller.get = get
    roller.replay_paths = lambda pid: []
    assert roller.roll("independent") is True
    assert calls == {LIST: 1, SIDEBAR: 3}


@pytest.mark.parametrize("fence", ["hold", "marker"])
def test_update_fence_arriving_during_selector_read_preserves_predecessor(home, fence):
    roller, host = native_roller(home)
    original = roller.get

    def get(port, path, timeout):
        result = original(port, path, timeout)
        if path == SIDEBAR:
            target = roller.marker if fence == "marker" else roller.marker.parent / "core-update/holds/hermes-sync"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        return result

    roller.get = get
    assert roller.roll("late fence") is False
    assert ro.read_active()["port"] == 9148
    assert 9148 in host.running


@pytest.mark.parametrize("failure", ["exception", "death"])
def test_after_publication_warm_failure_never_restores_or_stops_advertised_backend(home, failure):
    roller, host = native_roller(home)
    published = []

    def warm(port, paths, **kwargs):
        published.append(ro.active_path().read_bytes())
        if failure == "death":
            host.running.discard(port)
            return {}
        raise RuntimeError("fixture warm failure")

    roller.warm = warm
    assert roller.roll("post-promotion") is True
    assert published == [ro.active_path().read_bytes()]
    assert ro.read_active()["port"] == 9149
    assert not any(call[0] == "stop" for call in host.calls)


@pytest.mark.parametrize("policy", [False, None, 1, "true"])
def test_legacy_or_nonboolean_policy_keeps_warm_before_switch(home, policy):
    roller, _host = native_roller(home)
    original = roller.get
    canonical = []

    def get(port, path, timeout):
        status, elapsed, body = original(port, path, timeout)
        if path == "/api/health" and body:
            body["sessions_early_owner"] = policy
        if path == SIDEBAR:
            canonical.append(path)
        return status, elapsed, body

    def warm(port, paths):
        assert ro.read_active()["port"] == 9148
        return {}

    roller.get, roller.warm = get, warm
    assert roller.roll("legacy") is True
    assert canonical == []


# ── a busy successor is asked again, not abandoned (run64 canary, 23:36:50Z) ─────────────────────────


@pytest.mark.parametrize("probe,miss", [
    ("/api/host/identity", None), ("/api/host/identity", 503),
    ("/api/ssh/ownership", None), ("/api/ssh/ownership", 503),
])
def test_incarnation_probe_that_misses_once_is_retried(home, capsys, probe, miss):
    roller, host = native_roller(home)
    original = roller.get
    misses = [2]

    def get(port, path, timeout):
        if path == probe and misses[0] > 0:
            misses[0] -= 1
            return miss, timeout, None
        return original(port, path, timeout)

    roller.get = get
    roller.replay_paths = lambda pid: []
    assert roller.roll("busy successor") is True
    assert ro.read_active()["port"] == 9149
    assert not any(call[0] == "stop" for call in host.calls)
    err = capsys.readouterr().err
    assert f"incarnation pending ({probe} {miss})" in err
    assert "canonical sessions unavailable" not in err


def test_slow_unit_pid_while_unit_active_is_retried(home):
    roller, _host = native_roller(home)
    misses = [1]
    real = roller.unit_pid

    def unit_pid(port, timeout=15):
        if misses[0] > 0:
            misses[0] -= 1
            return 0  # systemctl show timed out under boot load
        return real(port, timeout=timeout)

    roller.unit_pid = unit_pid
    roller.replay_paths = lambda pid: []
    assert roller.roll("slow systemctl") is True
    assert ro.read_active()["port"] == 9149


def test_successor_that_never_answers_identity_is_abandoned_at_deadline(home):
    roller, _host = native_roller(home)
    original = roller.get
    roller.get = lambda port, path, timeout: ((None, timeout, None) if path == "/api/host/identity"
                                              else original(port, path, timeout))
    started = roller.clock()
    assert roller.roll("never answers") is False
    assert roller.clock() - started >= roller.boot_timeout_s - 1  # waited the bounded boot budget
    assert ro.read_active()["port"] == 9148
    assert not ro.read_active()["warming"]


def test_successor_unit_that_died_is_not_waited_for(home):
    roller, host = native_roller(home)
    original = roller.get

    def get(port, path, timeout):
        if path == "/api/host/identity":
            host.running.discard(port)  # the unit exited: MainPID 0, is-active inactive
            return None, timeout, None
        return original(port, path, timeout)

    roller.get = get
    started = roller.clock()
    assert roller.roll("died") is False
    assert roller.clock() - started < 1.0
    assert ro.read_active()["port"] == 9148


@pytest.mark.parametrize("selector", [LIST, SIDEBAR])
def test_real_store_selector_slower_than_fast_probe_still_promotes(home, selector):
    roller, _host = native_roller(home)
    original = roller.get
    timeouts = []

    def get(port, path, timeout):
        if path == selector:
            timeouts.append(timeout)
            if timeout < .6:  # a 471 KB canonical envelope took 0.23-0.57 s on the hub
                roller.sleep(timeout)
                return None, timeout, None
        return original(port, path, timeout)

    roller.get = get
    roller.replay_paths = lambda pid: []
    assert roller.roll("slow selector") is True
    assert timeouts[0] == pytest.approx(.25)
    assert timeouts[-1] >= .6
