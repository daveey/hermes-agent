"""Asynchronous handoffs cannot steal a newer turn's admission generation."""
import threading

import pytest

from hermes_cli import backend_retirement
from tui_gateway import turn_admission as ta

pytestmark = pytest.mark.platforms("posix")


def test_stale_stage_cannot_launch_worker_for_new_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    with ta.admission(session) as accepted:
        assert accepted
        session["running"] = True
    ran = threading.Event()
    try:
        with ta.stage(session):
            with session["history_lock"]:
                ta.settle(session)
                session["running"] = False
            with ta.admission(session) as accepted:
                assert accepted
                session["running"] = True
            thread = ta.start_work(ran.set, name="stale-handoff", session=session)
            if thread is not None:
                thread.join(5)
            assert thread is None
            assert not ran.is_set()
            assert session["running"]
        assert fence.active_count() == 1
    finally:
        with session["history_lock"]:
            ta.settle(session)
    assert fence.active_count() == 0


def test_stale_callback_retains_original_not_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    with ta.admission(session) as accepted:
        assert accepted
        session["running"] = True
    cancel = lambda: None
    try:
        with ta.stage(session):
            with session["history_lock"]:
                ta.settle(session)
                session["running"] = False
            with ta.admission(session) as accepted:
                assert accepted
                session["running"] = True
            callback, cancel = ta.retained_callback(session, lambda: ta.settle(session))
        assert fence.active_count() == 2
        assert callback() is False
        assert fence.active_count() == 1
        assert session["running"]
    finally:
        cancel()
        ta.settle(session)
    assert fence.active_count() == 0


def test_worker_exit_clears_running_after_mid_turn_settle(tmp_path, monkeypatch):
    """A turn settled mid-turn still unwinds its own ``running`` flag when its worker exits."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    with ta.admission(session) as accepted:
        assert accepted
        session["running"] = True
        thread = ta.start_work(lambda: _settle_mid_turn(session), name="mid-turn-settle", session=session)
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    assert session["running"] is False
    assert session.get("_turn_admission_generation") is None
    assert session.get("_turn_admission") is None
    assert fence.active_count() == 0


def _settle_mid_turn(session):
    with session["history_lock"]:
        ta.settle(session)
