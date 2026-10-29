"""Children inherit owning notify subscriptions only, never observer ones. [daveey]

A shepherd card collects one subscription per chat that watched it (hermes-hub ``shepherd`` -> platform
'hub', explicit ``notify-subscribe``, comment/unblock ``touch``). ``_inherit_notify_subs`` copied all of them
to every child, so each observer chat came to own the shepherd's whole program (runners-panel chat d48ade
showed 5 cards it never created). Children keep the owning subs (kanban_create's ``create``, the origin
binding's ``origin``, legacy rows written in the creating call) plus the creator's ``session_id``.
The DM-routing columns of what IS inherited stay covered by test_kanban_notify.py (issue #73030).
"""
import argparse
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_origin
from hermes_cli.kanban_db_graph import inherit_creator_origin


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _subscribe_cli(task_id, platform, chat_id):
    """The real ``hermes kanban notify-subscribe`` handler (what hermes-hub's ``shepherd`` action runs)."""
    ns = argparse.Namespace(task_id=task_id, platform=platform, chat_id=chat_id, chat_type=None,
                            thread_id=None, user_id=None, user_id_alt=None, notifier_profile="default",
                            delivery_mode=None, parent_chat_id=None, guild_id=None, json=False)
    assert kcli._cmd_notify_subscribe(ns) == 0


def _sub_keys(conn, task_id):
    return sorted((s["platform"], s["chat_id"]) for s in kbn.list_notify_subs(conn, task_id))


def _reasons(conn, task_id):
    return {s["chat_id"]: (s["delivery_metadata"] or {}).get("reason") for s in kbn.list_notify_subs(conn, task_id)}


OWNING = [("hub", "macbook/default/origin"), ("tui", "creator")]


def _shepherd_parent(observers):
    """A card created by tui chat 'creator' and origin-bound to macbook chat 'origin', then watched by
    ``observers`` hermes-hub shepherd chats, one explicit tui shepherd and one tui touch."""
    conn = kbc.connect()
    parent = kb.create_task(conn, title="shepherd", assignee="default", session_id="creator")
    kbn.add_notify_sub(conn, task_id=parent, platform="tui", chat_id="creator",
                       notifier_profile="default", delivery_metadata={"reason": "create"})
    kanban_origin.bind(conn, parent, kanban_origin.Origin("macbook", "default", "origin"))
    conn.close()
    for i in range(observers):
        _subscribe_cli(parent, "hub", f"macbook/default/observer{i}")
    _subscribe_cli(parent, "tui", "panel")
    conn = kbc.connect()
    kbn.add_notify_sub(conn, task_id=parent, platform="tui", chat_id="toucher",
                       notifier_profile="default", delivery_metadata={"reason": "touch"})
    return conn, parent


@pytest.mark.parametrize("path", ["creator", "parents", "link"])
def test_shepherd_observer_subs_not_inherited_by_child(kanban_home, path):
    conn, parent = _shepherd_parent(observers=40)
    try:
        assert len(kbn.list_notify_subs(conn, parent)) == 44
        if path == "creator":  # a worker of the shepherd card fans out (kanban_create from its run)
            child = kb.create_task(conn, title="fan-out", assignee="w", creator_task_id=parent)
            assert kb.get_task(conn, child).session_id == "creator"
        elif path == "parents":
            child = kb.create_task(conn, title="gated", assignee="w", parents=[parent])
        else:
            child = kb.create_task(conn, title="later", assignee="w")
            kb.link_tasks(conn, parent, child)
        assert _sub_keys(conn, child) == OWNING
        assert _reasons(conn, child) == {"creator": "create", "macbook/default/origin": "origin"}
    finally:
        conn.close()


def test_decompose_child_does_not_inherit_observer_subs(kanban_home):
    conn, parent = _shepherd_parent(observers=3)
    try:
        child = kb.create_task(conn, title="decomposed", assignee="w")
        with kb.write_txn(conn):
            inherit_creator_origin(conn, child, parent, created_at=int(time.time()))
        assert _sub_keys(conn, child) == OWNING
    finally:
        conn.close()


def test_grandchild_keeps_owning_chain(kanban_home):
    conn, parent = _shepherd_parent(observers=2)
    try:
        child = kb.create_task(conn, title="child", assignee="w", creator_task_id=parent)
        grandchild = kb.create_task(conn, title="grandchild", assignee="w", creator_task_id=child)
        assert _sub_keys(conn, grandchild) == OWNING
    finally:
        conn.close()


def test_legacy_hub_subs_inherit_only_when_bound_at_creation(kanban_home):
    """Hub rows written before provenance stamping carry no reason: the origin binding is written in the
    creating call, a hermes-hub shepherd only later."""
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="legacy", assignee="default")
        created = kb.get_task(conn, parent).created_at
        for chat, at in (("macbook/default/bound", created), ("macbook/default/late", created + 3600)):
            conn.execute(
                "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at) "
                "VALUES (?, 'hub', ?, '', ?)", (parent, chat, at))
        conn.commit()
        child = kb.create_task(conn, title="child", assignee="w", creator_task_id=parent)
        assert _sub_keys(conn, child) == [("hub", "macbook/default/bound")]
    finally:
        conn.close()


def test_legacy_unstamped_tui_and_messaging_subs_still_inherit(kanban_home):
    """Rows with no provenance outside 'hub' (pre-stamping tui creates, gateway DMs) keep the old behaviour."""
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="legacy", assignee="default")
        kbn.add_notify_sub(conn, task_id=parent, platform="tui", chat_id="old-chat")
        kbn.add_notify_sub(conn, task_id=parent, platform="telegram", chat_id="dm1", chat_type="dm",
                           delivery_mode="notify+wake")
        child = kb.create_task(conn, title="child", assignee="w", parents=[parent])
        assert _sub_keys(conn, child) == [("telegram", "dm1"), ("tui", "old-chat")]
    finally:
        conn.close()


def test_resubscribe_never_downgrades_an_owning_sub(kanban_home):
    conn = kbc.connect()
    parent = kb.create_task(conn, title="owned", assignee="default")
    kbn.add_notify_sub(conn, task_id=parent, platform="tui", chat_id="creator",
                       delivery_metadata={"reason": "create"})
    kanban_origin.bind(conn, parent, kanban_origin.Origin("macbook", "default", "origin"))
    kbn.add_notify_sub(conn, task_id=parent, platform="tui", chat_id="t", delivery_metadata={"reason": "touch"})
    conn.close()
    _subscribe_cli(parent, "tui", "creator")
    _subscribe_cli(parent, "hub", "macbook/default/origin")
    _subscribe_cli(parent, "tui", "t")  # touch -> shepherd is still an upgrade
    conn = kbc.connect()
    try:
        assert _reasons(conn, parent) == {"creator": "create", "macbook/default/origin": "origin", "t": "shepherd"}
    finally:
        conn.close()


def _legacy_hub(conn, task_id, chat, at):
    conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at) "
                 "VALUES (?, 'hub', ?, '', ?)", (task_id, chat, at))


@pytest.mark.parametrize("edge", ["creator", "link"])
def test_backfill_stops_legacy_observer_copies_spreading(kanban_home, edge):
    """Old inheritance copied a grandparent's hub observer onto the parent with the PARENT's creation time, so
    the copy looked like a fresh origin binding. The board-open backfill resolves copies through the graph."""
    conn = kbc.connect()
    try:
        grand = kb.create_task(conn, title="program", assignee="default")
        g_at = kb.get_task(conn, grand).created_at
        _legacy_hub(conn, grand, "macbook/default/origin", g_at)
        _legacy_hub(conn, grand, "macbook/default/observer", g_at + 3600)
        if edge == "creator":
            parent = kb.create_task(conn, title="worker fan-out", assignee="w", creator_task_id=grand)
        else:
            parent = kb.create_task(conn, title="linked", assignee="w")
            kb.link_tasks(conn, grand, parent)
        p_at = g_at + 7200  # created after the observer subscribed to the grandparent
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (p_at, parent))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (parent,))
        for chat in ("macbook/default/origin", "macbook/default/observer"):  # what the old code wrote
            _legacy_hub(conn, parent, chat, p_at)
        _legacy_hub(conn, parent, "macbook/default/late", p_at + 600)  # a shepherd of the parent itself
        conn.commit()
    finally:
        conn.close()
    kb.init_db()  # board open re-runs the migration pass
    conn = kbc.connect()
    try:
        assert _reasons(conn, grand) == {"macbook/default/origin": "origin", "macbook/default/observer": "shepherd"}
        assert _reasons(conn, parent) == {"macbook/default/origin": "origin", "macbook/default/observer": "shepherd",
                                          "macbook/default/late": "shepherd"}
        child = kb.create_task(conn, title="next", assignee="w", creator_task_id=parent)
        assert _sub_keys(conn, child) == [("hub", "macbook/default/origin")]
        assert kbn.stamp_legacy_hub_sub_provenance(conn) == 0  # idempotent: nothing left unstamped
    finally:
        conn.close()


def test_backfill_keeps_existing_metadata_and_skips_stamped_rows(kanban_home):
    conn = kbc.connect()
    try:
        task = kb.create_task(conn, title="t", assignee="default")
        at = kb.get_task(conn, task).created_at
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at, "
                     "delivery_metadata) VALUES (?, 'hub', 'gw/p/a', '', ?, '{\"guild_id\": \"g\"}')", (task, at))
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at, "
                     "delivery_metadata) VALUES (?, 'hub', 'gw/p/b', '', ?, 'not json')", (task, at + 999))
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at, "
                     "delivery_metadata) VALUES (?, 'hub', 'gw/p/c', '', ?, '{\"reason\": \"shepherd\"}')", (task, at))
        conn.commit()
        assert kbn.stamp_legacy_hub_sub_provenance(conn) == 2
        subs = {s["chat_id"]: s["delivery_metadata"] for s in kbn.list_notify_subs(conn, task)}
        assert subs == {"gw/p/a": {"guild_id": "g", "reason": "origin"}, "gw/p/b": {"reason": "shepherd"},
                        "gw/p/c": {"reason": "shepherd"}}
    finally:
        conn.close()


def test_board_open_survives_a_fenced_backfill(kanban_home, monkeypatch):
    """A delegated-child opener may not write: the backfill's PermissionError must not fail board open, and
    inheritance still drops a late unstamped hub row through its creation-window fallback."""
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="default")
        _legacy_hub(conn, parent, "gw/p/late", kb.get_task(conn, parent).created_at + 3600)
        conn.commit()
    finally:
        conn.close()

    def fenced(_conn):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")

    monkeypatch.setattr("hermes_cli.kanban_db_notify.stamp_legacy_hub_sub_provenance", fenced)
    kb.init_db()
    conn = kbc.connect()
    try:
        assert _reasons(conn, parent) == {"gw/p/late": None}
        child = kb.create_task(conn, title="c", assignee="w", creator_task_id=parent)
        assert _sub_keys(conn, child) == []
    finally:
        conn.close()


def test_backfill_ignores_ancestor_rows_written_after_the_row(kanban_home):
    """A copy is written after the row it copies: an ancestor's same-chat row written LATER is not the source,
    so a late unstamped shepherd on the parent stays a shepherd even when the ancestor's later row is owning."""
    conn = kbc.connect()
    try:
        grand = kb.create_task(conn, title="program", assignee="default")
        parent = kb.create_task(conn, title="fan-out", assignee="w", creator_task_id=grand)
        p_at = kb.get_task(conn, parent).created_at
        _legacy_hub(conn, parent, "macbook/default/x", p_at + 600)
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at, "
                     "delivery_metadata) VALUES (?, 'hub', 'macbook/default/x', '', ?, '{\"reason\": \"origin\"}')",
                     (grand, p_at + 5000))
        conn.commit()
        assert kbn.stamp_legacy_hub_sub_provenance(conn) == 1
        assert _reasons(conn, parent) == {"macbook/default/x": "shepherd"}
        child = kb.create_task(conn, title="next", assignee="w", creator_task_id=parent)
        assert _sub_keys(conn, child) == []
    finally:
        conn.close()
