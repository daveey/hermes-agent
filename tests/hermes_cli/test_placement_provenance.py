"""Foreign creator discovery never substitutes the executing worker namespace."""
import sqlite3

import pytest

from hermes_cli import kanban_placement_provenance as provenance


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE tasks(id TEXT, created_on TEXT);"
                       "CREATE TABLE task_runs(id INTEGER, task_id TEXT, uid TEXT);"
                       "INSERT INTO tasks VALUES ('t_creator','hub');"
                       "INSERT INTO task_runs VALUES (7,'t_creator','run-portable');")
    monkeypatch.setattr(provenance, "opened_board", lambda c: "research")
    monkeypatch.setattr(provenance.sync, "board_mode", lambda c: "primary")
    monkeypatch.setattr(provenance, "_replica_task_owners", lambda t, b: [("hub", "owner-profile")])
    yield conn
    conn.close()


def test_exact_foreign_owner_and_run(db):
    assert provenance.forwarding_authority(db, "t_creator", 7, self_gw="worker-7") == {
        "creator": {"gateway": "hub", "profile": "owner-profile", "board": "research", "task_id": "t_creator"},
        "run_uid": "run-portable"}


@pytest.mark.parametrize("owners", [[], [("other", "default")], [("hub", "a"), ("hub", "b")]])
def test_unknown_or_ambiguous_owner_never_uses_worker_profile(db, monkeypatch, owners):
    monkeypatch.setattr(provenance, "_replica_task_owners", lambda t, b: owners)
    with pytest.raises(ValueError, match="creator_unknown"):
        provenance.forwarding_authority(db, "t_creator", 7, self_gw="worker-7")


@pytest.mark.parametrize("change", ["UPDATE tasks SET created_on=NULL", "DELETE FROM tasks",
                                    "UPDATE task_runs SET task_id='t_other'", "UPDATE task_runs SET uid=NULL",
                                    "DELETE FROM task_runs"])
def test_unknown_owner_or_run_refuses(db, change):
    db.execute(change)
    with pytest.raises(ValueError, match="creator_unknown"):
        provenance.forwarding_authority(db, "t_creator", 7, self_gw="worker-7")


def test_local_owner_uses_board_namespace_not_active_profile(db, monkeypatch):
    db.execute("UPDATE tasks SET created_on='worker-7'")
    monkeypatch.setattr(provenance.sync, "_db_key", lambda c: "/evidenced/board.db")
    monkeypatch.setattr(provenance, "board_owner_profile", lambda p: "board-profile")
    result = provenance.forwarding_authority(db, "t_creator", 7, self_gw="worker-7")
    assert result["creator"]["profile"] == "board-profile"


def test_unsynced_run_is_not_authority(db, monkeypatch):
    monkeypatch.setattr(provenance.sync, "board_mode", lambda c: "off")
    with pytest.raises(ValueError, match="creator_unknown"):
        provenance.forwarding_authority(db, "t_creator", 7, self_gw="worker-7")
