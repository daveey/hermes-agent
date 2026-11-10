"""Fail-closed provenance boundaries independent of board state."""
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_session_provenance as provenance


@pytest.mark.parametrize("surface", ["desktop", "cli", "tui", "gui", "acp"])
def test_interactive_local_surfaces(surface):
    assert provenance.session_creation_source({"source": "local", "created_source": surface}) == surface
    assert surface in provenance._INTERACTIVE_SOURCES


@pytest.mark.parametrize("source,created,expected", [
    ("kanban", "desktop", "kanban"), ("desktop", "cron", "cron"), ("local", "oneshot", "oneshot"),
    ("local", "subagent", "subagent"), ("desktop", None, "desktop"), ("local", None, None),
])
def test_machine_creation_wins(source, created, expected):
    assert provenance.session_creation_source({"source": source, "created_source": created}) == expected


@pytest.mark.parametrize("ref,gateway,profile", [
    ("here/default/sid", "away", None), ("here/default/sid", "here", "research"),
    ("away/default/sid", None, None), ("here/default/sid/extra", None, None),
    ("here/sid", None, None), ("sid", "", None), ("", None, None),
])
def test_foreign_malformed_and_conflicting_refs_never_read_local_store(monkeypatch, ref, gateway, profile):
    def forbidden(*args, **kwargs):
        pytest.fail("foreign ref attempted local provenance lookup")
    monkeypatch.setattr(provenance, "_local_homes", forbidden)
    assert provenance.scoped_sources(gateway, [ref], profile=profile, local_gateway="here") == {ref: None}


def test_legacy_store_and_cache_expiry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "state.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE sessions(id TEXT, source TEXT)")
        db.execute("INSERT INTO sessions VALUES ('sid','desktop')")
        db.commit()
    now = [1.0]
    monkeypatch.setattr(provenance.time, "monotonic", lambda: now[0])
    provenance._source_cache.clear()
    assert provenance._local_session_sources(["sid", "missing"], home=home) == {"sid": "desktop", "missing": None}
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE sessions SET source='kanban'")
        db.commit()
    assert provenance._local_session_sources(["sid"], home=home) == {"sid": "desktop"}
    now[0] += provenance._MISSING_TTL_S + 1
    assert provenance._local_session_sources(["sid"], home=home) == {"sid": "kanban"}


def test_missing_and_unreadable_store_are_unknown_without_creation(tmp_path):
    provenance._source_cache.clear()
    assert provenance._local_session_sources(["sid"], home=tmp_path) == {"sid": None}
    assert not (tmp_path / "state.db").exists()
    (tmp_path / "state.db").write_text("not sqlite")
    assert provenance._local_session_sources(["sid"], home=tmp_path) == {"sid": None}
    assert not provenance._source_cache


def test_empty_batch_does_not_open_store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("empty batch opened a connection")
    monkeypatch.setattr(provenance.sqlite3, "connect", forbidden)
    assert provenance._local_session_sources([], home=tmp_path) == {}
