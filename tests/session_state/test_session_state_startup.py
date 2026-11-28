"""SessionState v2 serve startup: config switch, no state.db creation, clean stop. [daveey]"""
from __future__ import annotations

from hermes_cli import session_state_startup as startup
from hermes_session_state_store import current, install


def test_enabled_follows_config(monkeypatch):
    from hermes_cli import config

    for value, expected in ((True, True), (False, False), ("off", False), (None, True)):
        cfg = {} if value is None else {"session_state": {"enabled": value}}
        monkeypatch.setattr(config, "load_config_readonly", lambda cfg=cfg: cfg)
        assert startup.enabled() is expected


def test_start_without_a_state_db_creates_nothing(tmp_path):
    previous = install(None)
    try:
        assert startup.start(tmp_path) is None
        assert not (tmp_path / "state.db").exists() and current() is None
    finally:
        install(previous)


def test_start_and_stop_with_a_state_db(tmp_path, kanban_root, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    SessionDB(db_path=tmp_path / "state.db").close()
    previous = install(None)
    try:
        store = startup.start(tmp_path, tick_s=0.05)
        assert store is not None and current() is store
        other = tmp_path / "secondary"
        other.mkdir()
        token = set_hermes_home_override(str(other))
        try:
            assert current() is None  # a context bound to another profile home never sees this store
        finally:
            reset_hermes_home_override(token)
        assert current() is store
        startup.stop(store)
        assert current() is None
    finally:
        install(previous)
