"""Closed archived prerequisites are nonterminal in local and foreign dependency views."""
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_parent_refresh as refresh


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_remote_mirror_terminal_archive_does_not_satisfy(incremental, legacy, monkeypatch):
    from hermes_cli import kanban_move_parent_evidence as evidence
    live = sqlite3.connect(":memory:", isolation_level=None)
    replica = sqlite3.connect(":memory:", isolation_level=None)
    try:
        live.row_factory = sqlite3.Row
        live.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT);"
                           "CREATE TABLE task_links(parent_id TEXT,child_id TEXT);"
                           "CREATE TABLE kanban_sync_remote_parents(parent_id TEXT PRIMARY KEY,status TEXT,refreshed_at INTEGER);"
                           "INSERT INTO tasks VALUES ('child','todo');"
                           "INSERT INTO task_links VALUES ('held','child'),('ordinary','child');")
        marker = "" if legacy else ",containment_episode TEXT"
        replica.execute(f"CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT{marker})")
        if legacy:
            replica.execute("INSERT INTO tasks VALUES ('held','archived'),('ordinary','archived')")
        else:
            replica.execute("INSERT INTO tasks VALUES ('held','archived','episode'),('ordinary','archived',NULL)")
        monkeypatch.setattr(evidence, "prepare", lambda *a, **kw: ({}, {}))
        monkeypatch.setattr(evidence, "stored", lambda *a: {"held": None, "ordinary": None})
        if incremental:
            refresh._refresh_rows(live, replica, {"held", "ordinary"}, scoped={})
        else:
            sync.refresh_remote_parents(live, replica, scoped={})
        states = dict(kb.parent_states(live, "child", remote=True))
        assert states == {"held": "archived" if legacy else "triage", "ordinary": "archived"}
    finally:
        live.close()
        replica.close()
