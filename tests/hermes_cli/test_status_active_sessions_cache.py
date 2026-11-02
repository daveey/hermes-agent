"""``/api/status``'s active-session count re-reads ``state.db`` only when it changed. [daveey]

David-MBP profile (t_01a199a0a5d38255): every Desktop status poll opened ``state.db`` and ran ``list_sessions_rich``
(50 rows) to count sessions active in the last 5 minutes (~4-6 ms of CPU on the Mac). The rows are now kept on the
file signatures of ``state.db`` and its WAL, trusted only once settled (a write in the same timestamp tick as the
stat could keep the signature); the 5-minute window is applied afresh on every call.
"""

from __future__ import annotations

import time

import pytest

import utils
from hermes_cli.web_routers import status as st
from hermes_state import SessionDB, _default_db_path


@pytest.fixture
def home():
    home = _default_db_path().parent  # the hermetic conftest pins the store path
    home.mkdir(parents=True, exist_ok=True)
    st._reset_active_sessions_cache_for_tests()
    yield home
    st._reset_active_sessions_cache_for_tests()


@pytest.fixture
def opens(monkeypatch):
    calls = []
    real = st._open_session_db_for_profile

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(st, "_open_session_db_for_profile", spy)
    return calls


@pytest.fixture
def settle(monkeypatch):
    """Treat the store's files as past the racy window, as an idle Desktop poll sees them (``os.utime`` can't age
    a file: it moves the ctime to now)."""
    return lambda _home: monkeypatch.setattr(utils, "RACY_MTIME_WINDOW_NS", 0)


def test_unchanged_store_is_read_once(home, opens, settle):
    db = SessionDB(home / "state.db")
    db.create_session("s1", source="cli")  # held open, like the live gateway's store (its WAL persists)
    settle(home)
    assert [st._count_status_active_sessions() for _ in range(4)] == [1] * 4
    assert len(opens) == 1
    db.close()


def test_a_session_write_shows_on_the_next_poll(home, opens, settle):
    db = SessionDB(home / "state.db")
    db.create_session("s1", source="cli")
    settle(home)
    assert st._count_status_active_sessions() == 1
    db.create_session("s2", source="cli")
    assert st._count_status_active_sessions() == 2
    db.end_session("s1", "done")
    assert st._count_status_active_sessions() == 1
    db.close()


def test_a_racy_store_is_never_cached(home, opens):
    db = SessionDB(home / "state.db")
    db.create_session("s1", source="cli")  # just written: its mtime is inside the racy window
    st._count_status_active_sessions()
    st._count_status_active_sessions()
    assert len(opens) == 2
    db.close()


def test_the_activity_window_moves_with_the_clock(home, opens, settle, monkeypatch):
    db = SessionDB(home / "state.db")
    db.create_session("s1", source="cli")
    settle(home)
    assert st._count_status_active_sessions() == 1
    real = time.time
    monkeypatch.setattr(st.time, "time", lambda: real() + 3600)
    assert st._count_status_active_sessions() == 0
    assert len(opens) == 1
    db.close()
