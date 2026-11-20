"""Exact-profile live lookup at the real automation wake boundary."""
import threading

import pytest

from tui_gateway import automation_replay as ar, server


@pytest.mark.parametrize("profile", [None, "work"])
def test_wake_does_not_use_foreign_live_same_key(monkeypatch, tmp_path, profile):
    from hermes_constants import get_process_hermes_home, get_default_hermes_root
    home = get_process_hermes_home() if profile is None else get_default_hermes_root() / "profiles" / profile
    foreign = {"session_key": "same-key", "profile_home": str(tmp_path / "foreign"),
               "agent": object(), "history_lock": threading.Lock()}
    monkeypatch.setitem(server._sessions, "foreign-ui", foreign)
    monkeypatch.setattr(ar, "retiring_backend_refuses", lambda *a: False)
    monkeypatch.setattr("tui_gateway.update_pause.left_to_successor", lambda *a: False)
    resumed = []
    monkeypatch.setitem(server._methods, "session.resume", lambda rid, params:
                        resumed.append(params) or {"error": {"code": 4007}})
    # If the foreign live record is selected, fail before any runtime construction.
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: pytest.fail("foreign runtime selected"))
    try:
        assert server._wake_session_turn("same-key", "notice", profiles=[profile]) == "missing"
        assert resumed == [{"session_id": "same-key", "omit_messages": True,
                            **({"profile": profile} if profile else {})}]
        assert str(home) != foreign["profile_home"]
    finally:
        server._sessions.pop("foreign-ui", None)


def test_named_launch_matches_none_and_explicit_records(monkeypatch, tmp_path):
    from tui_gateway.wake_profiles import live_for_profiles
    home = tmp_path / "profiles" / "work"
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: home)
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: tmp_path)
    for recorded in (None, str(home)):
        session = {"session_key": "named-key", "profile_home": recorded}
        monkeypatch.setitem(server._sessions, "named-ui", session)
        assert live_for_profiles("named-key", [None]) == ("named-ui", session)
        assert live_for_profiles("named-key", ["work"]) == ("named-ui", session)
        assert live_for_profiles("named-key", ["other"]) is None


def test_tip_lookup_uses_exact_store_and_does_not_create_missing(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.wake_profiles import compression_live
    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: tmp_path)
    with SessionDB(home / "state.db") as db:
        db.create_session("root", "desktop")
    selected = []
    original = SessionDB.get_compression_tip

    def tip(db, key):
        selected.append(db.db_path)
        assert original(db, key) == "root"
        return "tip"

    monkeypatch.setattr(SessionDB, "get_compression_tip", tip)
    session = {"session_key": "tip", "profile_home": str(home)}
    monkeypatch.setitem(server._sessions, "tip-ui", session)
    assert compression_live("root", ["absent", "work"]) == ("tip-ui", session)
    assert selected == [home / "state.db"]
    assert not (tmp_path / "profiles" / "absent").exists()
