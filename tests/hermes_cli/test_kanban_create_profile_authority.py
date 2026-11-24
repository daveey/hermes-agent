"""Create authority is judged under the ACTUAL creating profile, never by searching all profiles.

A session registered only in profile ``builder``'s state.db is a valid owning chat for a create run AS that
profile, and must stay refused for a create run as ``default`` (no cross-profile proof). Inherited children of a
chat-owned creator keep durable authority after the creator completes, in the creating profile.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_create_authority import CreateAuthorityRefused, has_authority

SESSION = "20261111_193218_a850ab"


def _home(tmp_path, monkeypatch, profile):
    root = tmp_path / ".hermes"
    (root / "profiles" / "builder").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = root if profile == "default" else root / "profiles" / profile
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_session_activity as ksa
    ksa._reset_cache_for_tests()
    kb.init_db()
    return root


def _register(path, sid=SESSION):
    from hermes_state import SessionDB
    db = SessionDB(db_path=path / "state.db")
    try:
        db.create_session(session_id=sid, source="desktop")
    finally:
        db.close()


def _links(conn):
    from hermes_cli import kanban_db_sync as sync
    start = sync.SYNC_SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS task_chat_links")
    conn.executescript(sync.SYNC_SCHEMA_SQL[start:sync.SYNC_SCHEMA_SQL.index(";", start) + 1])


def _renews(conn, tid, session, profile):
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_scope_renewal_cli import resolve_actor
    return resolve_actor(conn, tid, writer=("agent", session, "1:HERMES_SESSION_ID"),
                         environs=[{"HERMES_SESSION_ID": session}], gateway=gateway_names()[0], profile=profile)


@pytest.mark.parametrize("profile", ["default", "builder"])
@pytest.mark.parametrize("default_collision", [False, True])
def test_registered_creation_authority_survives_reconcile(tmp_path, monkeypatch, profile, default_collision):
    from hermes_cli import kanban_chat_links, kanban_origin
    from hermes_cli.kanban_scope_renewal import RenewalRefused

    root = _home(tmp_path, monkeypatch, profile)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("fixture-gw", set()))
    _register(root if profile == "default" else root / "profiles" / profile)
    if profile != "default" and default_collision:
        _register(root)  # Identical bare session IDs in two profiles are not shared authority.
    with kbc.connect_closing() as conn:
        _links(conn)
        tid = kb.create_task(conn, title="direct", assignee="default", session_id=SESSION,
                             require_scope_authority=True)
        assert _renews(conn, tid, SESSION, profile).authority == "chat"
        for _ in range(2):
            kanban_chat_links.reconcile(conn, "default", "fixture-gw", ledger=False)
            assert _renews(conn, tid, SESSION, profile).authority == "chat"
            owners = list(conn.execute(
                "SELECT COALESCE(chat_profile,'default') FROM task_chat_links "
                "WHERE task_id=? AND kind='created' AND released=0", (tid,)))
            assert [r[0] for r in owners] == [profile]
            if profile != "default":
                with pytest.raises(RenewalRefused):
                    _renews(conn, tid, SESSION, "default")


@pytest.mark.parametrize("links", [False, True])
def test_session_registered_only_in_other_profile_is_refused_for_default(tmp_path, monkeypatch, links):
    root = _home(tmp_path, monkeypatch, "default")
    _register(root / "profiles" / "builder")
    with kbc.connect_closing() as conn:
        if links:
            _links(conn)
        before = list(conn.iterdump())
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="cross-profile", assignee="default", session_id=SESSION,
                           require_scope_authority=True)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("links", [False, True])
def test_session_registered_in_creating_profile_is_authority_and_child_survives_creator(tmp_path, monkeypatch, links):
    root = _home(tmp_path, monkeypatch, "builder")
    from hermes_cli import kanban_chat_links, kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("fixture-gw", set()))
    _register(root / "profiles" / "builder")
    with kbc.connect_closing() as conn:
        if links:
            _links(conn)
        direct = kb.create_task(conn, title="direct", assignee="default", session_id=SESSION,
                                require_scope_authority=True)
        assert has_authority(conn, direct, None, durable=True)
        assert _renews(conn, direct, SESSION, "builder").authority == "chat"
        parent = kb.create_task(conn, title="creator", assignee="default", session_id=SESSION)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        kb._append_event(conn, parent, "owner_admitted", {"session_id": "worker-session"})
        child = kb.create_task(conn, title="inherited child", assignee="default", creator_task_id=parent,
                               require_scope_authority=True)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        assert has_authority(conn, child, parent, durable=True)
        assert _renews(conn, child, SESSION, "builder").authority == "chat"
        if links:
            kanban_chat_links.reconcile(conn, "default", "fixture-gw", ledger=False)
            assert _renews(conn, direct, SESSION, "builder").authority == "chat"
            assert _renews(conn, child, SESSION, "builder").authority == "chat"
        from hermes_cli.kanban_scope_renewal import RenewalRefused
        with pytest.raises(RenewalRefused):  # the finished creator task itself is no authority
            _renews(conn, child, "worker-session", "builder")
