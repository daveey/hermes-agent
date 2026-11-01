"""Worker-created cards belong to human chats, not execution receipts. [daveey]"""
import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_chat_links as links
from tools import kanban_tools as tools

WORKER = "local-" + "ab" * 32
CHAT = "20261101_100000_human"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_ORIGIN_SESSION",
                "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    from hermes_cli import kanban_capability_registry as registry
    monkeypatch.setattr(registry, "self_gateway", lambda: "here")
    monkeypatch.setattr(tools, "_is_dispatcher_owned_worker", lambda: True)
    from agent import delegation_context
    monkeypatch.setattr(delegation_context, "is_dispatcher_owned_worker_context", lambda: True)
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    db.create_session(WORKER, source="kanban")
    db.create_session(CHAT, source="desktop")
    db.close()
    from tests.hermes_cli.kanban_sync_support import write_sync_config
    write_sync_config(home, True)
    kb.init_db()
    from hermes_cli import kanban_sync_client as sync
    with kbc.connect_closing() as conn:
        sync.ensure_identity(conn, "default", "here")
    return home


def create(entry, monkeypatch, capsys, origin=None):
    if entry == "tool":
        result = json.loads(tools._handle_create({"title": "child", "assignee": "default", "requires": [],
                                                "origin_session": origin, "placement": "local"}))
        assert result.get("ok"), result
        return result["task_id"]
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "create", "child", "--assignee", "default", "--json"]
                             + (["--origin-session", origin] if origin else []))
    assert cli._cmd_create(args) == 0
    return json.loads(capsys.readouterr().out)["id"]


@pytest.mark.parametrize("entry", ["tool", "cli"])
@pytest.mark.parametrize("gateway", ["away", "here"])
def test_explicit_origin_overrides_worker_inheritance(home, monkeypatch, capsys, entry, gateway):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away"}))
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="creator", session_id=CHAT)
        notify.add_notify_sub(conn, task_id=creator, platform="tui", chat_id=CHAT)
        links.reconcile(conn, "default", "here", ledger=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    child = create(entry, monkeypatch, capsys, origin=f"@session:research@{gateway}/explicit-chat")
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        expected_sub = ("hub", f"{gateway}/research/explicit-chat")
        assert [(s["platform"], s["chat_id"]) for s in notify.list_notify_subs(conn, child)] == [expected_sub]
        assert [tuple(r) for r in conn.execute(
            "SELECT chat_gateway,chat_profile,chat_session FROM task_chat_links WHERE task_id=? AND released=0",
            (child,))] == [(gateway, "research", "explicit-chat")]


@pytest.mark.parametrize("entry", ["tool", "cli"])
def test_unresolved_worker_records_gap_without_a_chat(home, monkeypatch, capsys, entry):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="unowned creator", assignee="default", session_id=WORKER)
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    child = create(entry, monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        assert not conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=?", (child,)).fetchall()
        assert not notify.list_notify_subs(conn, child)
        gaps = [e for e in kb.list_events(conn, child) if e.kind == "creator_chats_unresolved"]
        assert len(gaps) == 1
        assert gaps[0].payload["task_id"] == child
        assert gaps[0].payload["creator_task_id"] == creator


@pytest.mark.parametrize("entry", ["tool", "cli"])
def test_human_created_card_keeps_chat(home, monkeypatch, capsys, entry):
    monkeypatch.setattr(tools, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setenv("HERMES_SESSION_ID", CHAT)
    monkeypatch.setenv("HERMES_SESSION_KEY", CHAT)
    child = create(entry, monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        assert [(r[0], r[1]) for r in conn.execute(
            "SELECT chat_session, kind FROM task_chat_links WHERE task_id=? AND released=0", (child,))] == [
                (CHAT, "created")]


@pytest.mark.parametrize("entry", ["tool", "cli"])
@pytest.mark.parametrize("has_link", [True, False])
def test_worker_creation_resolves_remote_ancestor(home, monkeypatch, capsys, entry, has_link):
    with kbc.connect_closing() as conn:
        ancestor = kb.create_task(conn, title="ancestor", assignee="default")
        creator = kb.create_task(conn, title="creator", assignee="default", parents=[ancestor])
        if has_link:
            conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                         "released, created_at) VALUES (?, ?, 'away', 'research', ?, 'created', 0, 1)",
                         (links.link_uid(ancestor, "away", "research", CHAT, "created"), ancestor, CHAT))
            conn.commit()
        notify.add_notify_sub(conn, task_id=ancestor, platform="hub", chat_id=f"away/research/{CHAT}",
                              notifier_profile="default", delivery_mode="notify+wake",
                              delivery_metadata={"reason": "origin", "channel": "preserved"})
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    child = create(entry, monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        owned = conn.execute("SELECT chat_gateway, chat_profile, chat_session, kind FROM task_chat_links "
                             "WHERE task_id=? AND released=0", (child,)).fetchall()
        assert [tuple(r) for r in owned] == [("away", "research", CHAT, "created")]
        subs = notify.list_notify_subs(conn, child)
        assert [(s["platform"], s["chat_id"], s["delivery_mode"]) for s in subs] == [
            ("hub", f"away/research/{CHAT}", "notify+wake")]
        assert subs[0]["delivery_metadata"] == {"reason": "origin", "channel": "preserved"}
        assert not any(notify.is_headless_session(r[0]) for r in conn.execute(
            "SELECT chat_session FROM task_chat_links WHERE task_id=?", (child,)))


@pytest.mark.parametrize("entry", ["tool", "cli"])
def test_worker_creation_resolves_same_gateway_chat(home, monkeypatch, capsys, entry):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="creator", assignee="default")
        conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, ?, 'here', NULL, ?, 'created', 0, 1)",
                     (links.link_uid(creator, "here", None, CHAT, "created"), creator, CHAT))
        conn.commit()
        notify.add_notify_sub(conn, task_id=creator, platform="tui", chat_id=CHAT,
                              delivery_mode="wake", delivery_metadata={"reason": "create"})
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    child = create(entry, monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        owned = conn.execute("SELECT chat_session, kind FROM task_chat_links WHERE task_id=? AND released=0",
                             (child,)).fetchall()
        assert [(r[0], r[1]) for r in owned] == [(CHAT, "created")]
        subs = notify.list_notify_subs(conn, child)
        assert [(s["platform"], s["chat_id"], s["delivery_mode"]) for s in subs] == [("tui", CHAT, "wake")]
        assert not any(notify.is_headless_session(r[0]) for r in conn.execute(
            "SELECT chat_session FROM task_chat_links WHERE task_id=?", (child,)))
