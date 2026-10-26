"""Successful scoped poll receipts, not subscription/thread-name guesses."""
import threading
from tui_gateway import kanban_consumer_health as health


def test_only_registered_scoped_live_success_counts(tmp_path, monkeypatch):
    stop = threading.Event()
    ready = threading.Event()
    trigger = threading.Event()
    home = tmp_path / "profile"
    board = home / "kanban.db"
    session = {"session_key": "origin", "profile_home": str(home)}
    def run():
        trigger.wait(3)
        health.polled(home, "origin", board)
        ready.set()
        stop.wait(3)
    thread = threading.Thread(target=run)
    health.register(session, stop, thread)
    try:
        thread.start()
        assert not health.available(home, "origin", board)
        trigger.set()
        assert ready.wait(3)
        assert health.available(home, "origin", board)
        assert not health.available(home, "other", board)
        assert not health.available(tmp_path / "other", "origin", board)
        assert not health.available(home, "origin", home / "other.db")
        session["session_key"] = "rotated-origin"
        assert not health.available(home, "rotated-origin", board)
        session["session_key"] = "origin"
        session["profile_home"] = str(tmp_path / "other")
        assert not health.available(tmp_path / "other", "origin", board)
        session["profile_home"] = str(home)
        session["_finalized"] = True
        assert not health.available(home, "origin", board)
        session["_finalized"] = False
        session["_kanban_refused_until"] = health.time.monotonic() + 60
        assert not health.available(home, "origin", board)
        session["_kanban_refused_until"] = 0
        monkeypatch.setattr(health, '_MAX_AGE', -1)
        assert not health.available(home, "origin", board)
        stop.set()
        assert not health.available(home, "origin", board)
    finally:
        stop.set()
        thread.join(3)
    assert not health.available(home, "origin", board)


def test_foreign_thread_cannot_forge_success(tmp_path):
    stop = threading.Event()
    thread = threading.Thread(target=lambda: stop.wait(3))
    session = {"session_key": "origin", "profile_home": str(tmp_path)}
    health.register(session, stop, thread)
    try:
        thread.start()
        health.polled(tmp_path, "origin", tmp_path / "kanban.db")
        assert not health.available(tmp_path, "origin", tmp_path / "kanban.db")
    finally:
        stop.set()
        thread.join(3)
