"""First physical Anthropic request after a relay pull gap uses the applied token."""
from __future__ import annotations

import json
import sys
import time
import subprocess
from types import SimpleNamespace

import pytest

from tests.agent import test_anthropic_pool_pin as pins

home = pins.home


def _receipt(home, checked_at=None):
    path = home / "state" / "oauth-relay-success.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"version": 1, "providers": {"anthropic": {
        "checked_at": time.time() - 1000 if checked_at is None else checked_at,
        "interval_s": 120,
    }}}))
    return path


def _relay_agent(home):
    data = json.loads((home / "auth.json").read_text())
    for row in data["credential_pool"]["anthropic"]:
        row.update(source="manual:hub_minted", refresh_token=None,
                   expires_at_ms=int(time.time() * 1000) + 3600000)
    (home / "auth.json").write_text(json.dumps(data))
    return pins._pinned_agent()


def test_first_request_after_gap_uses_applied_token(home, monkeypatch, caplog):
    agent = _relay_agent(home)
    receipt = _receipt(home)
    # A real child applies auth then publishes success. No provider request/network.
    script = home / "pull.py"
    script.write_text('''import fcntl, json, os, pathlib, sys, time
home = pathlib.Path(sys.argv[1])
assert "PYTHONPATH" not in os.environ and "PYTHONHOME" not in os.environ
with (home / "auth.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
p = home / "auth.json"
d = json.loads(p.read_text())
for row in d["credential_pool"]["anthropic"]:
    if row["id"] == "acct-B": row["access_token"] = "new-relay-token"
p.write_text(json.dumps(d))
r = home / "state" / "oauth-relay-success.json"
d = json.loads(r.read_text())
d["providers"]["anthropic"]["checked_at"] = time.time()
r.write_text(json.dumps(d))
(home / "pulled").write_text("yes")
''')
    monkeypatch.setenv("PYTHONPATH", "/fixture/foreign-python")
    monkeypatch.setenv("PYTHONHOME", "/fixture/foreign-python")
    monkeypatch.setattr("agent.credential_relay_repull.pull_command",
                        lambda: [sys.executable, str(script), str(home)])
    pool = agent._credential_pool
    sync = pool._sync_entry_from_pool_store
    def sync_after_unlock(entry):
        import fcntl
        with (home / "state" / "oauth-relay-repull.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return sync(entry)
    monkeypatch.setattr(pool, "_sync_entry_from_pool_store", sync_after_unlock)
    assert pins._request_key(agent) == "new-relay-token"
    assert json.loads(receipt.read_text())["providers"]["anthropic"]["checked_at"] > time.time() - 10
    assert "401" not in caplog.text
    assert agent._credential_pool_entry_id == "acct-B"


def test_failed_pull_is_coalesced_across_sessions(home, monkeypatch):
    from agent import credential_relay_preflight as pre
    agent = _relay_agent(home)
    _receipt(home)
    calls = []
    monkeypatch.setattr("agent.credential_relay_repull.pull_command", lambda: ["fake-pull"])
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: calls.append(k) or SimpleNamespace(returncode=1))
    assert pins._request_key(agent) == pins.B_1
    assert pins._request_key(_relay_agent(home)) == pins.B_1
    assert len(calls) == 1


@pytest.mark.parametrize("age", [0, 239, 240, -3600])
def test_fresh_or_future_receipt_never_pulls(home, monkeypatch, age):
    agent = _relay_agent(home)
    from agent import credential_relay_preflight as pre
    monkeypatch.setattr(pre, "time", SimpleNamespace(time=lambda: 10000))
    _receipt(home, 10000 - age)
    monkeypatch.setattr("agent.credential_relay_repull.pull_command", lambda: pytest.fail("fresh pull"))
    assert pins._request_key(agent) == pins.B_1


@pytest.mark.parametrize("data", [None, "broken", [], {"version": 2,
    "providers": {"anthropic": {"checked_at": 1, "interval_s": 120}}},
    {"version": 1, "providers": []}, {"version": 1, "providers": {"openai-codex": {}}},
    *[{"version": 1, "providers": {"anthropic": {"checked_at": value, "interval_s": 120}}}
      for value in [None, "123", True, 0, -1, float("nan"), float("inf")]],
    *[{"version": 1, "providers": {"anthropic": {"checked_at": 1, "interval_s": value}}}
      for value in [None, "120", True, 0, -1, float("nan"), float("inf")]]])
def test_unknown_receipt_does_not_start_pull(home, monkeypatch, data):
    agent = _relay_agent(home)
    path = _receipt(home)
    if data is None:
        path.unlink()
    else:
        path.write_text(data if isinstance(data, str) else json.dumps(data))
    from agent.credential_relay_preflight import _receipt as read_receipt
    assert read_receipt(path, "anthropic") is None
    monkeypatch.setattr("agent.credential_relay_repull.pull_command", lambda: pytest.fail("unknown pull"))
    assert pins._request_key(agent) == pins.B_1


@pytest.mark.parametrize("source,refresh,auth_type", [
    ("manual", None, "oauth"), ("manual:hub_minted", "owned", "oauth"),
    ("manual:hub_minted", None, "api_key"), ("claude_code", None, "oauth")])
def test_other_credentials_do_not_inspect_receipts(home, monkeypatch, source, refresh, auth_type):
    from agent import credential_relay_preflight as pre
    monkeypatch.setattr(pre, "_receipt", lambda *a: pytest.fail("unrelated credential"))
    pre.before_pinned_request("anthropic", SimpleNamespace(
        source=source, refresh_token=refresh, auth_type=auth_type))


class _Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.elapsed

    def time(self):
        return 10000 + self.elapsed

    def sleep(self, duration):
        self.elapsed += duration
        self.on_sleep()


def _controlled(home, monkeypatch):
    from agent import credential_relay_preflight as pre
    agent = _relay_agent(home)
    _receipt(home, 9000)
    clock = _Clock()
    monkeypatch.setattr(pre, "time", clock)
    monkeypatch.setattr("agent.credential_relay_repull.pull_command", lambda: ["fake-pull"])
    return pre, agent, clock


def test_async_kickstart_waits_for_applied_receipt(home, monkeypatch):
    pre, agent, clock = _controlled(home, monkeypatch)
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))

    def apply():
        data = json.loads((home / "auth.json").read_text())
        data["credential_pool"]["anthropic"][1]["access_token"] = "async-token"
        (home / "auth.json").write_text(json.dumps(data))
        _receipt(home, clock.time())

    clock.on_sleep = apply
    assert pins._request_key(agent) == "async-token"
    assert clock.elapsed > 0


def test_async_receipt_timeout_fails_open_with_one_deadline(home, monkeypatch):
    pre, agent, clock = _controlled(home, monkeypatch)
    calls = []

    def kick(*a, **kw):
        calls.append(kw)
        clock.elapsed += 12
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pre.subprocess, "run", kick)
    assert pins._request_key(agent) == pins.B_1
    assert clock.elapsed == pytest.approx(30)
    assert len(calls) == 1 and calls[0]["timeout"] <= 30


def test_lock_wait_consumes_command_budget(home, monkeypatch):
    import fcntl
    pre, agent, clock = _controlled(home, monkeypatch)
    calls = []

    def flock(*args):
        if clock.elapsed < 10:
            raise BlockingIOError()

    def run(*args, **kw):
        calls.append(kw)
        raise subprocess.TimeoutExpired("fake-pull", kw["timeout"])

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(pre.subprocess, "run", run)
    assert pins._request_key(agent) == pins.B_1
    assert 0 < calls[0]["timeout"] <= 20


def test_busy_lock_times_out_without_command(home, monkeypatch):
    import fcntl
    pre, agent, clock = _controlled(home, monkeypatch)

    def busy(*args):
        raise BlockingIOError()

    monkeypatch.setattr(fcntl, "flock", busy)
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: pytest.fail("locked pull"))
    assert pins._request_key(agent) == pins.B_1
    assert clock.elapsed == pytest.approx(30)


def test_peer_success_rechecked_after_lock(home, monkeypatch):
    import fcntl
    pre, agent, clock = _controlled(home, monkeypatch)
    monkeypatch.setattr(fcntl, "flock", lambda *args: _receipt(home, clock.time()))
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: pytest.fail("duplicate pull"))
    assert pins._request_key(agent) == pins.B_1


def test_no_command_fails_open(home, monkeypatch):
    pre, agent, _clock = _controlled(home, monkeypatch)
    monkeypatch.setattr("agent.credential_relay_repull.pull_command", lambda: None)
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: pytest.fail("no command"))
    assert pins._request_key(agent) == pins.B_1


def test_failed_attempt_is_retried_after_cooldown(home, monkeypatch):
    pre, agent, clock = _controlled(home, monkeypatch)
    calls = []
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: calls.append(k) or SimpleNamespace(returncode=1))
    assert pins._request_key(agent) == pins.B_1
    clock.elapsed = 30
    assert pins._request_key(agent) == pins.B_1
    assert len(calls) == 1  # failed pulls do not consume 30s on every next request
    clock.elapsed = 240
    assert pins._request_key(agent) == pins.B_1
    assert len(calls) == 2


@pytest.mark.parametrize("contents", ["[]", "broken"])
def test_bad_attempt_history_does_not_prevent_catchup(home, monkeypatch, contents):
    pre, agent, _clock = _controlled(home, monkeypatch)
    (home / "state" / "oauth-relay-repull.lock").write_text(contents)
    calls = []
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: calls.append(k) or SimpleNamespace(returncode=1))
    assert pins._request_key(agent) == pins.B_1
    assert len(calls) == 1


@pytest.mark.parametrize("stage", ["lock", "env"])
def test_expired_total_budget_never_starts_command(home, monkeypatch, stage):
    import fcntl
    pre, agent, clock = _controlled(home, monkeypatch)
    if stage == "lock":
        monkeypatch.setattr(fcntl, "flock", lambda *a: setattr(clock, "elapsed", 30))
    else:
        def env(**kwargs):
            clock.elapsed = 30
            return {}
        monkeypatch.setattr("tools.environments.local.build_subprocess_env", env)
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: pytest.fail("expired budget"))
    assert pins._request_key(agent) == pins.B_1
    assert pre._wait(clock.monotonic()) is False
    if stage == "lock":
        assert (home / "state" / "oauth-relay-repull.lock").read_text() == ""


def test_receipt_disappears_while_waiting_for_lock(home, monkeypatch):
    import fcntl
    pre, agent, _clock = _controlled(home, monkeypatch)
    monkeypatch.setattr(fcntl, "flock", lambda *a: (home / "state" / "oauth-relay-success.json").unlink())
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: pytest.fail("unknown receipt"))
    assert pins._request_key(agent) == pins.B_1


def test_command_error_fails_open(home, monkeypatch):
    pre, agent, _clock = _controlled(home, monkeypatch)
    def fail(*a, **k):
        raise OSError("fixture error")
    monkeypatch.setattr(pre.subprocess, "run", fail)
    assert pins._request_key(agent) == pins.B_1


def test_disabled_during_pull_is_not_handed_out(home, monkeypatch):
    from agent.credential_pool_disabled import set_credential_disabled
    from agent.credential_pool_pin import pinned_anthropic_pool_token
    pre, agent, _clock = _controlled(home, monkeypatch)
    pool = agent._credential_pool
    entry = next(e for e in pool.entries() if e.id == "acct-B")
    def run(*a, **k):
        set_credential_disabled("anthropic", entry, True)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(pre.subprocess, "run", run)
    assert pinned_anthropic_pool_token(pool, entry.id) == (False, None)


def test_no_receipt_backoff_grows_and_is_bounded(home, monkeypatch):
    pre, agent, clock = _controlled(home, monkeypatch)
    calls = []
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: calls.append(k) or SimpleNamespace(returncode=0))
    start = 0
    for cooldown in [240, 480, 960, 1920, 3600, 3600]:
        clock.elapsed = start
        before = len(calls)
        assert pins._request_key(agent) == pins.B_1
        assert len(calls) == before + 1
        assert clock.elapsed == pytest.approx(start + 30)
        clock.elapsed = start + cooldown - 1
        assert pins._request_key(_relay_agent(home)) == pins.B_1
        assert len(calls) == before + 1
        assert clock.elapsed == start + cooldown - 1
        start += cooldown


def test_backoff_is_provider_scoped_and_migrates_legacy_history(home, monkeypatch):
    pre, _agent, clock = _controlled(home, monkeypatch)
    path = home / "state" / "oauth-relay-repull.lock"
    path.write_text(json.dumps({"anthropic": clock.time(), "openai-codex": 9876}))
    with path.open("a+") as lock:
        assert not pre._claim_attempt(lock, "anthropic", 240, 9000)
        clock.elapsed = 240
        assert pre._claim_attempt(lock, "anthropic", 240, 9000)
        for start in [480, 960]:
            clock.elapsed = start
            assert pre._claim_attempt(lock, "anthropic", 240, 9000)
        assert not pre._claim_attempt(lock, "anthropic", 240, 9000)
        assert pre._claim_attempt(lock, "openai-codex", 240, 9000)
        assert not pre._claim_attempt(lock, "anthropic", 240, 9000)
    data = json.loads(path.read_text())
    assert data["anthropic"]["count"] == 3
    assert data["openai-codex"]["count"] == 1


def test_new_success_resets_backoff_even_if_aged_before_request(home, monkeypatch):
    pre, agent, clock = _controlled(home, monkeypatch)
    calls = []
    monkeypatch.setattr(pre.subprocess, "run", lambda *a, **k: calls.append(k) or SimpleNamespace(returncode=0))
    for start in [0, 240, 720]:
        clock.elapsed = start
        assert pins._request_key(agent) == pins.B_1
    # A real producer success between requests, already stale on the next call.
    _receipt(home, 10760)
    clock.elapsed = 1010
    assert pins._request_key(agent) == pins.B_1
    assert len(calls) == 4
    clock.elapsed = 1250
    assert pins._request_key(agent) == pins.B_1
    assert len(calls) == 5  # reset to base, not the previous 960s cooldown


def test_success_for_another_provider_does_not_complete_wait(home, monkeypatch):
    pre, agent, clock = _controlled(home, monkeypatch)
    path = home / "state" / "oauth-relay-success.json"
    def run(*a, **k):
        data = json.loads(path.read_text())
        data["providers"]["openai-codex"] = {"checked_at": clock.time(), "interval_s": 30}
        path.write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(pre.subprocess, "run", run)
    assert pins._request_key(agent) == pins.B_1
    assert clock.elapsed == pytest.approx(30)
