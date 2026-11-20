"""Local retirement custody is isolated before either profile hands off."""
import threading

import pytest

from tui_gateway import automation_replay as ar
from tui_gateway.automation_replay_handoff import Journal


@pytest.fixture
def homes(tmp_path, monkeypatch):
    import sqlite3
    roots = [tmp_path / "profiles" / p for p in ("alpha", "beta")]
    for home in roots:
        home.mkdir(parents=True)
        sqlite3.connect(home / "state.db").close()
    monkeypatch.setattr(ar, "ENABLED", True)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: roots[0])
    timers = []

    class Timer:
        def __init__(self, delay, callback):
            self.callback = callback
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(threading, "Timer", Timer)
    ar.reset()
    yield roots, timers
    ar.reset()


def schedule(home, text, *, persisted=True, now=100):
    return ar.schedule("same-key", text=text, display_kind="process_complete", display_metadata={},
                       due=1000, cause="quota", persisted=persisted, now=now, profile_home=home)


def test_same_key_two_profiles_handoff_without_coalescing(homes):
    (alpha, beta), _ = homes
    schedule(alpha, "alpha work")
    schedule(beta, "beta work")
    assert ar.handoff_all() == 2
    for home, expected in ((alpha, "alpha work"), (beta, "beta work")):
        batch = Journal(home).claim("same-key", 2000, "successor", lambda _: False)
        assert batch is not None
        import json
        assert [i["text"] for i in json.loads(batch["payload"])] == [expected]


def test_stop_one_profile_does_not_drop_other_profile_work(homes):
    (alpha, beta), _ = homes
    schedule(alpha, "alpha work", persisted=False)
    schedule(beta, "beta work", persisted=False)
    ar.cancel_durable(alpha, ("same-key",), 200, stop=True)
    assert ar.handoff_all() == 1
    assert Journal(alpha).claim("same-key", 2000, "a", lambda _: False) is None
    assert Journal(beta).claim("same-key", 2000, "b", lambda _: False) is not None


def test_timer_replays_only_its_profile(homes, monkeypatch):
    (alpha, beta), timers = homes
    schedule(alpha, "alpha work")
    schedule(beta, "beta work")
    calls = []
    def wake(key, text, **kw):
        calls.append((text, kw))
        assert kw["replay_delivery"].admit()
        return "started"

    monkeypatch.setattr("tui_gateway.server._wake_session_turn", wake)
    timers[0].callback()
    assert len(calls) == 1 and "alpha work" in calls[0][0] and "beta work" not in calls[0][0]
    assert calls[0][1]["profiles"] == ["alpha"]
    assert ar.handoff_all() == 1
    assert Journal(beta).claim("same-key", 2000, "b", lambda _: False) is not None


def test_success_only_cancels_carried_items_at_context_start(homes):
    (alpha, beta), _ = homes
    schedule(alpha, "already carried", now=100)
    schedule(alpha, "joined during turn", now=300)
    schedule(beta, "foreign profile", now=100)
    assert ar.cancel_carried("same-key", profile_home=alpha, through=200)
    assert ar.handoff_all() == 2
    import json
    for home, expected in ((alpha, "joined during turn"), (beta, "foreign profile")):
        batch = Journal(home).claim("same-key", 2000, "successor", lambda _: False)
        assert [i["text"] for i in json.loads(batch["payload"])] == [expected]
