"""[daveey] A turn-admission reservation file names its holder: session, work and thread (2026-11-20).

hermes-sync refused the hub's core update for 30+ min over "1 unpublished reservation(s)" and nothing said whose.
``admission_fence state-details`` now reads back the session key, the work name and the thread that reserved.
"""

from __future__ import annotations

import threading

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tui_gateway import turn_admission

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    return tmp_path


def _records(home):
    return af.state(home, details=True)["reservation_records"]


def test_acquire_records_session_work_and_thread(home):
    token = turn_admission.acquire(str(home), session={"session_key": "20261119_220010_0fbe00"},
                                   work="auto-continue-b9ae3e9c")
    assert token is not None
    try:
        [record] = _records(home)
        assert record["purpose"] == "turn_admission"
        assert record["session"] == "20261119_220010_0fbe00"
        assert record["work"] == "auto-continue-b9ae3e9c"
        assert record["thread"] == threading.current_thread().name
    finally:
        token.release()
    assert _records(home) == []


def test_prepared_turn_work_names_its_reservation(home):
    session = {"session_key": "chat-key", "history_lock": threading.RLock(), "profile_home": str(home)}
    work = turn_admission.prepare_work(lambda: None, name="prompt-turn-ui-sid", session=session)
    assert work is not None
    try:
        [record] = _records(home)
        assert (record["session"], record["work"]) == ("chat-key", "prompt-turn-ui-sid")
    finally:
        work.cancel()
    assert _records(home) == []


def test_a_turn_claim_names_its_session(home):
    session = {"session_key": "claimed-chat", "history_lock": threading.RLock(), "profile_home": str(home)}
    with turn_admission.admission(session) as admitted:
        assert admitted
        [record] = _records(home)
        assert (record["session"], record["work"]) == ("claimed-chat", "turn-claim")
