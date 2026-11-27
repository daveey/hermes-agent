"""Deterministic admission/profile and coalesced-publication regressions."""
import pytest

from tui_gateway import server, turn_outcome


@pytest.mark.parametrize("profile_home", [None, "foreign"])
def test_start_preserves_profile_routing(monkeypatch, tmp_path, profile_home):
    queued, observed = [], []
    home = tmp_path if profile_home is None else tmp_path / profile_home
    session = {"session_key": "profile-row", "profile_home": None if profile_home is None else str(home)}

    class QueuedThread:
        def __init__(self, *, target, **kwargs):
            queued.append(target)

        def start(self):
            pass

    monkeypatch.setattr("hermes_cli.local_work_threads.TrackedThread", QueuedThread)
    monkeypatch.setattr(server, "_session_home", lambda s: home)
    monkeypatch.setattr(turn_outcome, "_refresh_session_outcome", lambda s, *a: observed.append(s.copy()))
    turn_outcome.refresh_turn_start_outcome(session)
    session["profile_home"] = "mutated-after-admission"
    queued.pop()()
    assert observed[0]["profile_home"] == (None if profile_home is None else str(home))


def test_coalesced_start_reruns_after_stale_publication(monkeypatch, tmp_path):
    """A newer admission during an old publication must get a fresh read, not be dropped."""
    queued, observed = [], []
    session = {"session_key": "coalesced-row", "generation": "A"}

    class QueuedThread:
        def __init__(self, *, target, **kwargs):
            queued.append(target)

        def start(self):
            pass

    def refresh(snapshot, sid, trigger, concluding):
        observed.append(snapshot["generation"])
        if len(observed) == 1:
            # An old read/CAS retry can complete after this newer start request.
            session["generation"] = "B"
            turn_outcome.refresh_turn_start_outcome(session)
            turn_outcome.refresh_turn_start_outcome(session)

    monkeypatch.setattr("hermes_cli.local_work_threads.TrackedThread", QueuedThread)
    monkeypatch.setattr(server, "_session_home", lambda s: tmp_path)
    monkeypatch.setattr(turn_outcome, "_refresh_session_outcome", refresh)
    turn_outcome.refresh_turn_start_outcome(session)
    queued.pop()()
    assert observed == ["A", "B"]
    assert queued == []
    assert (str(tmp_path), "coalesced-row") not in turn_outcome._start_refresh_pending
