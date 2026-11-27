"""Scheduling contracts independent of expensive outcome source collection."""
from types import SimpleNamespace

from tui_gateway import server, turn_outcome


def test_start_refresh_coalesces_and_freezes_profile_and_sid(monkeypatch, tmp_path):
    calls, queued = [], []
    monkeypatch.setattr(server, "_session_home", lambda s: s["profile_home"])
    monkeypatch.setattr(turn_outcome, "_refresh_session_outcome",
                        lambda session, *args: calls.append((session["profile_home"], args)))

    class Queued:
        def __init__(self, target, **kw):
            self.target = target
            assert kw["daemon"] is True
            assert kw["source"] == "tui.turn_start_outcome"

        def start(self):
            queued.append(self.target)

    monkeypatch.setattr("hermes_cli.local_work_threads.TrackedThread", Queued)
    home = tmp_path / "one"
    session = {"session_key": "fallback", "profile_home": home}
    agent = SimpleNamespace(session_id="admitted")
    try:
        turn_outcome.refresh_turn_start_outcome(session, agent)
        turn_outcome.refresh_turn_start_outcome(session, agent)
        assert len(queued) == 1
        session["profile_home"] = tmp_path / "two"
        turn_outcome.refresh_turn_start_outcome(session, agent)
        assert len(queued) == 2, "same SID in another profile is independent"
        agent.session_id = "replacement"
        for publish in queued[:]:
            publish()
        assert calls == [(home, ("admitted", "turn_start", frozenset())),
                         (session["profile_home"], ("admitted", "turn_start", frozenset()))]
        turn_outcome.refresh_turn_start_outcome(session, agent)
        assert len(queued) == 3
        queued[-1]()
        turn_outcome.refresh_turn_start_outcome({})
        assert len(queued) == 3
    finally:
        turn_outcome._start_refresh_pending.clear()


def test_start_failure_is_best_effort_and_does_not_poison_retry(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(server, "_session_home", lambda s: tmp_path)
    attempts = []

    class Rejected:
        def __init__(self, **kw):
            pass

        def start(self):
            attempts.append(1)
            raise RuntimeError("admission refused")

    monkeypatch.setattr("hermes_cli.local_work_threads.TrackedThread", Rejected)
    session = {"session_key": "retry-scheduling"}
    turn_outcome.refresh_turn_start_outcome(session)
    turn_outcome.refresh_turn_start_outcome(session)
    assert attempts == [1, 1]
    assert (str(tmp_path), "retry-scheduling") not in turn_outcome._start_refresh_pending
    assert "outcome scheduling failed" in caplog.text


def test_refresh_failure_releases_coalescing_slot(monkeypatch, tmp_path):
    queued = []
    monkeypatch.setattr(server, "_session_home", lambda s: tmp_path)

    class Queued:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            queued.append(self.target)

    def fail(*a):
        raise ValueError("collector failure")

    monkeypatch.setattr("hermes_cli.local_work_threads.TrackedThread", Queued)
    monkeypatch.setattr(turn_outcome, "_refresh_session_outcome", fail)
    import pytest
    session = {"session_key": "collector-retry"}
    turn_outcome.refresh_turn_start_outcome(session)
    with pytest.raises(ValueError, match="collector failure"):
        queued.pop()()
    assert (str(tmp_path), "collector-retry") not in turn_outcome._start_refresh_pending
