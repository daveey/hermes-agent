"""Requirement D: an agent/CLI create must record scope authority, or be refused (nothing is written).

Incident: a card with created_by='claude-code-chat', session_id NULL and no creator task could never be renewed
(``renew-scope`` -> not_scope_authority); the DB only recorded ``creator_chats_unresolved``. System/internal
creators (detectors, human dashboard) keep their semantics, and ``created_by`` is never authority.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_create_authority import CreateAuthorityRefused



@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM",
                "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_SOURCE", "HERMES_RPC_SOCKET", "HERMES_KERNEL_SENTINEL"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_session_activity as ksa
    ksa._reset_cache_for_tests()
    kb.init_db()
    return h


def _seed_session(home, sid, source="desktop"):
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=sid, source=source)
    finally:
        db.close()


def _cli_create(title, *extra):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc._cmd_create(root.parse_args(["kanban", "create", title, *extra]))


def _titles():
    with kbc.connect_closing() as conn:
        return [t.title for t in kb.list_tasks(conn)]


def test_agent_cli_create_without_authority_is_refused(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_ID", "not_a_real_session")  # agent-invoked, no registered chat
    assert _cli_create("incident card", "--created-by", "claude-code-chat") == 2
    assert "scope_authority_missing" in capsys.readouterr().err
    assert "incident card" not in _titles()  # rolled back, not recorded as creator_chats_unresolved


def test_agent_cli_create_with_registered_session_records_authority(home, monkeypatch, capsys):
    _seed_session(home, "20261014_141400_abc123")
    monkeypatch.setenv("HERMES_SESSION_ID", "20261014_141400_abc123")
    assert _cli_create("owned") == 0
    with kbc.connect_closing() as conn:
        task = next(t for t in kb.list_tasks(conn) if t.title == "owned")
        assert task.session_id == "20261014_141400_abc123"


def test_human_terminal_create_keeps_human_authority(home, capsys):
    assert _cli_create("by a human") == 0
    assert "by a human" in _titles()


def test_db_requires_creator_task_or_session(home):
    with kbc.connect_closing() as conn:
        with pytest.raises(CreateAuthorityRefused) as err:
            kb.create_task(conn, title="no authority", assignee="default", created_by="claude-code-chat",
                           require_scope_authority=True)
        assert err.value.code == "scope_authority_missing"
        assert conn.execute("SELECT count(*) FROM tasks WHERE title='no authority'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='creator_chats_unresolved'").fetchone()[0] == 0
        # Mere creator existence is not renewable authority (renew-scope requires a running admitted creator).
        parent = kb.create_task(conn, title="creator", assignee="default", created_by="user")
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="child", assignee="default", created_by="claude-code-chat",
                           creator_task_id=parent, require_scope_authority=True)
        # a machine/placeholder session is not
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="cron card", assignee="default", created_by="x",
                           session_id="cron_deadbeef_20261014", require_scope_authority=True)


def test_internal_creators_keep_semantics(home):
    """System/detector creation never opts in: no authority needed, nothing invented from created_by."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="detector card", assignee="default", created_by="dup-detector")
        assert kb.get_task(conn, tid).session_id is None
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='creator_chats_resolved'",
                            (tid,)).fetchone()[0] == 0


def test_kanban_create_tool_refuses_authority_less_agent(home, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kt._handle_create({"title": "tool incident", "assignee": "default"}))
    assert "scope_authority_missing" in json.dumps(out) or "scope authority" in json.dumps(out)
    assert "tool incident" not in _titles()


def test_explicit_origin_chat_anchor_without_registered_session_is_authority(home, monkeypatch, capsys):
    """A validated --origin-session writes an unreleased ``created`` link: renewable authority for a foreign-gateway chat with no registered
    session row in state.db (platform/chat anchor)."""
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away"}))
    monkeypatch.setenv("HERMES_SESSION_ID", "not_a_real_session")
    origin = "@session:research@away/explicit-chat"
    with kbc.connect_closing() as conn:
        _links_schema(conn)
    assert _cli_create("anchored", "--origin-session", origin) == 0
    with kbc.connect_closing() as conn:
        tid = next(t.id for t in kb.list_tasks(conn) if t.title == "anchored")
        assert conn.execute("SELECT count(*) FROM kanban_notify_subs WHERE task_id=? AND chat_id LIKE ?",
                            (tid, "away/research/%")).fetchone()[0] == 1
        assert _actor(conn, tid, "explicit-chat", gateway="away", profile="research").authority == "chat"


def _links_schema(conn):
    """The production sync DDL, without enabling sync or reaching a Board."""
    from hermes_cli import kanban_db_sync as sync
    start = sync.SYNC_SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS task_chat_links")
    conn.executescript(sync.SYNC_SCHEMA_SQL[start:sync.SYNC_SCHEMA_SQL.index(";", start) + 1])


def _actor(conn, tid, session, *, creator=None, gateway=None, profile="default"):
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_scope_renewal_cli import resolve_actor
    env = {"HERMES_SESSION_ID": session}
    if creator:
        env["HERMES_KANBAN_TASK"] = creator
    return resolve_actor(conn, tid, writer=("agent", session, "4242:HERMES_SESSION_ID"), environs=[env],
                         gateway=gateway or gateway_names()[0], profile=profile)


@pytest.mark.parametrize("status,admitted", [("ready", False), ("running", False), ("done", True), ("running", True)])
def test_creator_must_be_running_and_have_renewable_admitted_identity(home, status, admitted):
    """A creator task alone (no chat ancestry) is transient authority: refused in every state, since once it
    completes nothing could renew the child. Nothing is written."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="bare creator", assignee="default", created_by="user")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, parent))
        if admitted:
            kb._append_event(conn, parent, "owner_admitted", {"session_id": "worker-session"})
        conn.commit()
        before = list(conn.iterdump())
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="unrenewable child", assignee="default", creator_task_id=parent,
                           require_scope_authority=True)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("links", [False, True])
def test_creator_with_chat_ancestry_gives_child_durable_authority_after_creator_ends(home, links):
    """The child inherits the creator's authentic owning chat; that chat can still renew once the creator is done."""
    session = "20261014_141400_d00d01"
    _seed_session(home, session)
    with kbc.connect_closing() as conn:
        if links:
            _links_schema(conn)
        parent = kb.create_task(conn, title="chat-owned creator", assignee="default", session_id=session)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        kb._append_event(conn, parent, "owner_admitted", {"session_id": "worker-session"})
        child = kb.create_task(conn, title="durable child", assignee="default", creator_task_id=parent,
                               require_scope_authority=True)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        assert _actor(conn, child, session).authority == "chat"
        with pytest.raises(Exception):  # the finished creator task itself is no authority
            _actor(conn, child, "worker-session", creator=parent)


def test_released_creator_chat_is_not_durable_authority(home):
    from hermes_cli.kanban_create_authority import has_authority
    session = "20261014_141400_d00d02"
    _seed_session(home, session)
    with kbc.connect_closing() as conn:
        _links_schema(conn)
        parent = kb.create_task(conn, title="creator", assignee="default", session_id=session)
        from hermes_cli.kanban_chat_links import link_uid
        conn.execute("DELETE FROM task_chat_links WHERE task_id=?", (parent,))
        conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,"
                     "created_at) VALUES (?,?,?,?,?,'created',1,1)",
                     (link_uid(parent, "", None, session, "created"), parent, "", None, session))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=?", (parent,))
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        kb._append_event(conn, parent, "owner_admitted", {"session_id": "worker-session"})
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="no live chat", assignee="default", creator_task_id=parent,
                           require_scope_authority=True)
        assert not has_authority(conn, parent, None, durable=True)


@pytest.mark.parametrize("released", [False, True])
def test_decompose_preserves_chat_release_and_atomic_refusal(home, monkeypatch, released):
    from hermes_cli.kanban_create_authority import has_authority
    from hermes_cli.kanban_db_graph import decompose_triage_task

    session = "20261123_110000_feed01"
    _seed_session(home, session)
    monkeypatch.setenv("HERMES_SESSION_ID", "unregistered-agent")
    with kbc.connect_closing() as conn:
        _links_schema(conn)
        root = kb.create_task(conn, title="triage root", assignee="default", triage=True,
                              session_id=session, require_scope_authority=True)
        if released:
            conn.execute("UPDATE task_chat_links SET released=1 WHERE task_id=?", (root,))
            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=?", (root,))
        conn.commit()
        assert has_authority(conn, root, None, durable=True) is (not released)
        before = list(conn.iterdump())

        def decompose():
            return decompose_triage_task(
                conn, root, root_assignee="default", auto_promote=False,
                children=[{"title": "child", "assignee": "default"}])

        if released:
            with pytest.raises(CreateAuthorityRefused) as err:
                decompose()
            assert err.value.code == "scope_authority_missing"
            assert list(conn.iterdump()) == before  # no children, links, subs, or root transition
        else:
            children = decompose()
            assert children is not None
            assert len(children) == 1
            assert has_authority(conn, children[0], None, durable=True)
            assert _actor(conn, children[0], session).authority == "chat"


@pytest.mark.parametrize("session", ["", "unknown-session", "kanban-placement", "cron_deadbeef_20261014"])
def test_unproven_raw_session_never_substitutes_for_authority(home, session):
    with kbc.connect_closing() as conn:
        before = list(conn.iterdump())
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="unproven raw session", assignee="default", session_id=session,
                           require_scope_authority=True)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("links", [False, True])
def test_registered_chat_is_renewable_immediately_without_later_reconcile(home, links):
    session = "20261014_141400_cafe00"
    _seed_session(home, session)
    with kbc.connect_closing() as conn:
        if links:
            _links_schema(conn)
        tid = kb.create_task(conn, title="registered owner", assignee="default", session_id=session,
                             require_scope_authority=True)
        assert _actor(conn, tid, session).authority == "chat"


@pytest.mark.parametrize("kind,released,session,profile,gateway,expected", [
    ("created", 0, "platform-chat", "research", "away", True),
    ("created", 0, "local-" + "a" * 64, "research", "away", True),
    ("created", 0, "local-" + "a" * 64, None, "away", False),
    ("created", 1, "platform-chat", "research", "away", False),
    ("shepherd", 0, "platform-chat", "research", "away", False),
    ("touch", 0, "platform-chat", "research", "away", False),
    ("created", 0, "cron_deadbeef_20261014", "research", "away", False),
    ("created", 0, "", "research", "away", False),
    ("created", 0, "kanban-placement", "research", "away", False),
    ("created", 0, "local-" + "a" * 64, "default", "here", False),
])
def test_link_authority_matches_renewal_and_foreign_provenance(home, monkeypatch, kind, released, session,
                                                             profile, gateway, expected):
    from hermes_cli import kanban_origin
    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_create_authority import has_authority
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away"}))
    with kbc.connect_closing() as conn:
        _links_schema(conn)
        tid = kb.create_task(conn, title="link fixture", assignee="default")
        conn.execute("INSERT INTO task_chat_links "
                     "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES (?,?,?,?,?,?,?,1)",
                     (link_uid(tid, gateway, profile, session, kind), tid, gateway, profile, session, kind, released))
        conn.commit()
        assert has_authority(conn, tid, None) is expected
        if expected:
            assert _actor(conn, tid, session, gateway=gateway, profile=profile).authority == "chat"


def test_foreign_origin_sub_without_link_is_not_renewable_on_legacy_board(home, monkeypatch):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away"}))
    with kbc.connect_closing() as conn:
        before = list(conn.iterdump())
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="legacy foreign anchor", assignee="default",
                           chat_origin="@session:research@away/platform-chat", require_scope_authority=True)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("source", ["kanban", "cron", "oneshot", "subagent"])
@pytest.mark.parametrize("session", ["ordinary-id", "local-" + "b" * 64])
def test_registered_machine_provenance_never_becomes_chat_authority(home, source, session):
    _seed_session(home, session, source=source)
    with kbc.connect_closing() as conn:
        before = list(conn.iterdump())
        with pytest.raises(CreateAuthorityRefused):
            kb.create_task(conn, title="machine is not owner", assignee="default", session_id=session,
                           require_scope_authority=True)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("links", [False, True])
def test_registered_local_hash_is_interactive_when_store_proves_it(home, links):
    session = "local-" + "c" * 64
    _seed_session(home, session)
    with kbc.connect_closing() as conn:
        if links:
            _links_schema(conn)
        tid = kb.create_task(conn, title="interactive local hash", assignee="default", session_id=session,
                             require_scope_authority=True)
        assert _actor(conn, tid, session).authority == "chat"


@pytest.mark.parametrize("reason", ["shepherd", "touch", None])
def test_legacy_observer_subscriptions_are_not_authority(home, reason):
    from hermes_cli.kanban_create_authority import has_authority
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="observer fixture", assignee="default")
        conn.execute("INSERT INTO kanban_notify_subs "
                     "(task_id,platform,chat_id,thread_id,delivery_metadata,created_at,last_event_id) "
                     "VALUES (?,'tui','observer','',?,1,0)", (tid, json.dumps({"reason": reason})))
        assert not has_authority(conn, tid, None)


def test_creator_argument_must_match_recorded_creator_and_not_childs_own_session(home):
    from hermes_cli.kanban_create_authority import has_authority
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="admitted creator", assignee="default")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        kb._append_event(conn, parent, "owner_admitted", {"session_id": "worker"})
        unrelated = kb.create_task(conn, title="unrelated child", assignee="default")
        assert not has_authority(conn, unrelated, parent)
        child = kb.create_task(conn, title="actual child", assignee="default", creator_task_id=parent)
        assert has_authority(conn, child, parent)
        kb._append_event(conn, child, "owner_admitted", {"session_id": "worker"})
        assert not has_authority(conn, child, parent)
