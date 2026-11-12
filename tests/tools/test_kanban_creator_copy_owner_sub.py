"""A worker running a picked-up copy files a child: the child's owner chat gets a sub, not only a link. [daveey]

Worker-3 evidence 2026-11-12: hub card t_01a1ced43b8a9675 ran on worker-3 as a picked-up copy
(``kanban_worker_node.import_cards``). The copy carried the creator's unreleased ``created`` link to
``hub/default/20261111_200009_edbe39`` but none of its ``kanban_notify_subs`` (those never sync). Its worker filed
t_01a1cee93a2ecae6: ``resolve_chats`` found the owner in the local file, so ``inherit_chats`` took the copy path
(``_copy_delivery``), which copies the creator's matching sub and found none. The child got the link and no sub, so
the hub collector published only its creating worker session (``rel: created``) and the owning chat no shepherd.
Worker-4's t_01a1cf56f1fd6e73 (creator copy of hub card t_01a1cf477dece56b) is the same case.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_chat_links as links
from tests.tools.test_kanban_worker_created_chats import CHAT, WORKER, create, make_home


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away"}))
    return home


def _copy_with_link(conn, gateway, profile, created_on="away"):
    """A creator card whose link rows are in this file but whose subs are not (a picked-up or synced copy of
    another gateway's card when ``created_on`` names that gateway)."""
    creator = kb.create_task(conn, title="picked-up creator", assignee="default")
    if not any(c[1] == "created_on" for c in conn.execute("PRAGMA table_info(tasks)")):
        conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
    conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (created_on, creator))
    conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                 "released, created_at) VALUES (?, ?, ?, ?, ?, 'created', 0, 1)",
                 (links.link_uid(creator, gateway, profile, CHAT, "created"), creator, gateway, profile, CHAT))
    conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=?", (creator,))
    conn.commit()
    return creator


def _subs(conn, task_id):
    return [(s["platform"], s["chat_id"], (s["delivery_metadata"] or {}).get("reason"))
            for s in notify.list_notify_subs(conn, task_id)]


@pytest.mark.parametrize("entry", ["tool", "cli"])
@pytest.mark.parametrize("gateway,profile,expected", [
    ("away", "research", ("hub", f"away/research/{CHAT}", "create")),
    ("here", None, ("tui", CHAT, "create")),
])
def test_child_of_a_copy_gets_its_owners_sub(home, monkeypatch, capsys, entry, gateway, profile, expected):
    with kbc.connect_closing() as conn:
        creator = _copy_with_link(conn, gateway, profile)
        assert _subs(conn, creator) == []
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    child = create(entry, monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        owned = conn.execute("SELECT chat_gateway, chat_session FROM task_chat_links WHERE task_id=? "
                             "AND kind='created' AND released=0", (child,)).fetchall()
        assert [tuple(r) for r in owned] == [(gateway, CHAT)]
        assert _subs(conn, child) == [expected]


def test_owner_sub_in_this_file_is_still_copied_once(home, monkeypatch, capsys):
    """The local owner's own delivery columns win; the fallback adds no second row."""
    with kbc.connect_closing() as conn:
        creator = _copy_with_link(conn, "away", "research")
        notify.add_notify_sub(conn, task_id=creator, platform="hub", chat_id=f"away/research/{CHAT}",
                              delivery_mode="notify+wake", delivery_metadata={"reason": "origin", "keep": 1})
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    child = create("tool", monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        subs = notify.list_notify_subs(conn, child)
        assert [(s["platform"], s["chat_id"], s["delivery_mode"], s["delivery_metadata"]) for s in subs] == [
            ("hub", f"away/research/{CHAT}", "notify+wake", {"reason": "origin", "keep": 1})]


@pytest.mark.parametrize("created_on", [None, "here"])
@pytest.mark.parametrize("gateway,profile", [("away", "research"), ("here", None)])
def test_own_card_whose_chat_unsubscribed_gives_its_child_no_sub(home, monkeypatch, capsys, created_on,
                                                                 gateway, profile):
    """This gateway's own owner card is authoritative: a chat that removed its sub there is not re-subscribed on
    the card's new worker children, although its unreleased ``created`` link still names it. [daveey]"""
    with kbc.connect_closing() as conn:
        creator = _copy_with_link(conn, gateway, profile, created_on=created_on)
        assert _subs(conn, creator) == []
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    monkeypatch.setenv("HERMES_SESSION_ID", WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", WORKER)
    child = create("tool", monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        assert _subs(conn, child) == []
