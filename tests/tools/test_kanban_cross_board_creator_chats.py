"""A worker's card reaches its creator's human chat even when the creator card lives on another board file. [daveey]

Two cross-board shapes left hub cards with ``creator_chats_unresolved`` after #544:

* Worker node: a MacBook card leased to the hub runs there, and its worker files a card on the hub's live board.
  The copied creator carries no chat links and is evicted when it ends, so only the replica
  (``sync-shadow/<board>.db``, the whole board) can walk to its chat.
* Placement: a worker routes its card to another gateway (``kanban.placement``). The receiving gateway never sees
  the creator card, so the creating side must send the resolved chat as the card's origin, never the worker
  transcript.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_chat_links as links
from tests.tools.test_kanban_worker_created_chats import CHAT, WORKER, create, make_home
from tools import kanban_tools as tools

FOREIGN_CHAT = "20261029_180356_84db5f"


@pytest.fixture
def home(tmp_path, monkeypatch):
    return make_home(tmp_path, monkeypatch)


def _link(conn, task_id, gateway, profile, session, kind="created"):
    conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                 "released, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
                 (links.link_uid(task_id, gateway, profile, session, kind), task_id, gateway, profile, session, kind))
    conn.commit()


def _replica(home_dir, name="default.db"):
    """The board replica with a MacBook creator chain whose ancestor holds the human chat."""
    path = home_dir / "kanban" / "sync-shadow" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    kbc.init_db(path)
    with kbc.connect_closing(path) as conn:
        ancestor = kb.create_task(conn, title="macbook ancestor", assignee="default")
        creator = kb.create_task(conn, title="macbook creator leased here", assignee="default", parents=[ancestor])
        _link(conn, ancestor, "macbook", None, FOREIGN_CHAT)
        _link(conn, creator, "macbook", None, "local-" + "cd" * 32)  # its worker transcript: never an owner
    return creator


@pytest.mark.parametrize("entry", ["tool", "cli"])
def test_worker_node_child_resolves_creator_chat_from_replica(home, monkeypatch, capsys, entry):
    creator = _replica(home)
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    child = create(entry, monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        owned = conn.execute("SELECT chat_gateway, chat_session, kind FROM task_chat_links "
                             "WHERE task_id=? AND released=0", (child,)).fetchall()
        assert [tuple(r) for r in owned] == [("macbook", FOREIGN_CHAT, "created")]
        subs = notify.list_notify_subs(conn, child)
        assert [(s["platform"], s["chat_id"]) for s in subs] == [("hub", f"macbook/default/{FOREIGN_CHAT}")]
        latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (child,)).fetchone()[0]
        assert subs[0]["last_event_id"] == latest  # no replay of the card's history
        kinds = [e.kind for e in kb.list_events(conn, child)]
        assert "creator_chats_resolved" in kinds and "creator_chats_unresolved" not in kinds


def test_backfill_repairs_an_unresolved_worker_node_card_from_replica(home, monkeypatch):
    creator = _replica(home, "pending.db")  # not yet the board's replica: the child is filed unresolved
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="hub child filed before the fix", assignee="default",
                               creator_task_id=creator, session_id=WORKER)
        assert [e.kind for e in kb.list_events(conn, child)].count("creator_chats_unresolved") == 1
    shadow = home / "kanban" / "sync-shadow"
    for suffix in ("", "-wal", "-shm"):
        if (shadow / f"pending.db{suffix}").exists():
            (shadow / f"pending.db{suffix}").rename(shadow / f"default.db{suffix}")
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        owned = conn.execute("SELECT chat_gateway, chat_session FROM task_chat_links WHERE task_id=? AND released=0 "
                             "AND chat_gateway='macbook'", (child,)).fetchall()
        assert [tuple(r) for r in owned] == [("macbook", FOREIGN_CHAT)]
        sub = [s for s in notify.list_notify_subs(conn, child) if s["platform"] == "hub"]
        latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (child,)).fetchone()[0]
        assert [(s["chat_id"], s["last_event_id"]) for s in sub] == [(f"macbook/default/{FOREIGN_CHAT}", latest)]


@pytest.fixture
def placed(home, monkeypatch):
    from hermes_cli import kanban_placement as kp
    sent = []

    def fake_create_remote(args, *, target, origin_session, origin_profile, self_gw, cfg=None, hub_cli=None):
        sent.append({"target": target, "origin_session": origin_session, "origin_profile": origin_profile})
        return {"ok": True, "request_id": "placement:hub:test", "queued_only": True}

    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "hub", "why": "test route"})
    monkeypatch.setattr(kp, "create_remote", fake_create_remote)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    return sent


def _place(creator, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    out = json.loads(tools._handle_create({"title": "routed child", "assignee": "default", "requires": []}))
    assert out.get("ok") is True, out
    return out


@pytest.mark.parametrize("profile", [None, "research"])
def test_placed_worker_child_carries_the_creator_chat_as_origin(placed, monkeypatch, profile):
    with kbc.connect_closing() as conn:
        ancestor = kb.create_task(conn, title="ancestor", assignee="default")
        creator = kb.create_task(conn, title="creator", assignee="default", parents=[ancestor],
                                 session_id=WORKER)
        _link(conn, ancestor, "here", profile, CHAT)
    _place(creator, monkeypatch)
    assert placed == [{"target": "hub", "origin_session": CHAT, "origin_profile": profile or "default"}]


def test_placed_worker_child_of_an_unresolved_creator_has_no_origin(placed, monkeypatch):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="unowned creator", assignee="default", session_id=WORKER)
    _place(creator, monkeypatch)
    assert placed == [{"target": "hub", "origin_session": None, "origin_profile": "default"}]


@pytest.mark.parametrize("order", [("away", "here"), ("here", "away")])
def test_placed_mixed_owners_prefer_verified_local_chat(placed, monkeypatch, order):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="mixed creator", assignee="default", session_id=WORKER)
        for gateway in order:
            _link(conn, creator, gateway, None, CHAT)
    _place(creator, monkeypatch)
    assert placed == [{"target": "hub", "origin_session": CHAT, "origin_profile": "default"}]


@pytest.mark.parametrize("local_session", [None, "unpersisted-local"])
def test_unverified_local_does_not_replace_first_foreign(home, local_session):
    from tools.kanban_tools_placement import _placement_origin

    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="foreign owners", assignee="default", session_id=WORKER)
        _link(conn, creator, "away", "research", FOREIGN_CHAT)
        if local_session:
            _link(conn, creator, "here", None, local_session)
        _link(conn, creator, "other", None, CHAT)
        assert _placement_origin(conn, creator, WORKER) == {
            "session_id": FOREIGN_CHAT, "origin_profile": "research", "origin_gateway": "away"}


def test_placed_foreign_chat_without_creator_authority_refuses(placed, monkeypatch):
    """A foreign chat must never be silently dropped or stamped with this worker."""
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="creator", assignee="default", session_id=WORKER)
        _link(conn, creator, "away", None, CHAT)
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    out = json.loads(tools._handle_create({"title": "routed child", "assignee": "default", "requires": []}))
    assert out.get("created") is False, out
    assert "origin_forward_creator_unknown" in out["error"]
    assert placed == []


def test_deferred_worker_child_records_the_creator_chat_for_its_forward(placed, monkeypatch):
    """A card kept here until its parents finish is forwarded later with the origin recorded now."""
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_forward as kpf
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "local", "defer": "hub", "why": "test defer"})
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="creator", assignee="default", session_id=WORKER)
        _link(conn, creator, "here", None, CHAT)
    out = _place(creator, monkeypatch)
    with kbc.connect_closing() as conn:
        st = kpf.state(conn, out["task_id"])
    assert (st["target"], st["origin_session"], st["origin_profile"]) == ("hub", CHAT, "default")
    assert placed == []


def test_deferred_worker_child_of_an_unresolved_creator_records_no_origin(placed, monkeypatch):
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_forward as kpf
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "local", "defer": "hub", "why": "test defer"})
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="unowned creator", assignee="default", session_id=WORKER)
    out = _place(creator, monkeypatch)
    with kbc.connect_closing() as conn:
        st = kpf.state(conn, out["task_id"])
    assert (st["target"], st["origin_session"]) == ("hub", None)
