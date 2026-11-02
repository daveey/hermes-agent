"""Machine sessions (worker/cron/one-shot/subagent transcripts) never own a card, whatever their id shape. [daveey]

``is_headless_session`` only knows ``local-<sha256>`` ids. Older Kanban workers, cron runs and one-shots have
ordinary timestamp ids (``20261029_180356_84db5f``) or ``cron_<job>_<ts>``, so ``resolve_chats`` took their
transcripts as the nearest human chat: hub t_01a19864fc42f5b2 (#594's live proof) was linked to a MacBook worker
transcript instead of emitting ``creator_chats_unresolved``.
"""
from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_creator_chats as creator_chats
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from tests.tools.test_kanban_worker_created_chats import CHAT, make_home

OLD_WORKER = "20261029_180356_84db5f"  # source=kanban, a pre-receipt worker transcript
CRON_RUN = "20261030_010000_c0ffee"  # source=cron with a timestamp id
ONESHOT = "20261030_020000_0e5407"  # source=oneshot
SUBAGENT = "20261030_030000_5ba9e7"  # source=subagent
CRON_ID = "cron_backend-timeouts-watch_20261101_0300"  # no state.db row at all


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    for sid, source in ((OLD_WORKER, "kanban"), (CRON_RUN, "cron"), (ONESHOT, "oneshot"), (SUBAGENT, "subagent")):
        db.create_session(sid, source=source)
    db.close()
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away", "hub"}))
    getattr(notify, "_source_cache", {}).clear()
    return home


def _link(conn, task_id, session, gateway="here", profile: str | None = "default", kind="created", released=0):
    conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                 "released, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                 (links.link_uid(task_id, gateway, profile, session, kind), task_id, gateway, profile, session, kind,
                  released))
    conn.commit()


def _sessions(owners):
    return [(o["chat_gateway"], o["chat_session"]) for o in owners]


@pytest.mark.parametrize("machine", [OLD_WORKER, CRON_RUN, ONESHOT, SUBAGENT])
def test_a_nearest_local_machine_link_resolves_to_the_human_chat_above(home, machine):
    """(a) The creator's own link is a local timestamp-id machine session: the human chat one level up owns."""
    with kbc.connect_closing() as conn:
        ancestor = kb.create_task(conn, title="human ancestor", assignee="default")
        creator = kb.create_task(conn, title="worker-owned creator", assignee="default", parents=[ancestor])
        _link(conn, ancestor, CHAT)
        _link(conn, creator, machine)
        assert _sessions(creator_chats.resolve_chats(conn, creator)) == [("here", CHAT)]
        child = kb.create_task(conn, title="child", assignee="default", creator_task_id=creator)
        owned = conn.execute("SELECT chat_session FROM task_chat_links WHERE task_id=? AND released=0",
                             (child,)).fetchall()
        assert [r[0] for r in owned] == [CHAT]


def test_a_hub_sub_branch_skips_a_local_machine_origin(home):
    """(a) ``_chat_links``' hub-sub branch: a legacy ``origin`` sub naming a local worker transcript owns nothing."""
    with kbc.connect_closing() as conn:
        ancestor = kb.create_task(conn, title="human ancestor", assignee="default")
        creator = kb.create_task(conn, title="worker-origin creator", assignee="default", parents=[ancestor])
        _link(conn, ancestor, CHAT, gateway="away")
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, delivery_mode, "
                     "delivery_metadata, created_at, last_event_id) VALUES (?, 'hub', ?, '', 'notify', ?, 1, 0)",
                     (creator, f"here/default/{OLD_WORKER}", json.dumps({"reason": "origin"})))
        conn.commit()
        assert _sessions(creator_chats.resolve_chats(conn, creator)) == [("away", CHAT)]


def test_a_child_does_not_inherit_a_machine_sessions_owning_sub(home):
    """(a) ``_inherit_notify_subs``: a parent's owning ``tui`` sub on a worker transcript is not copied."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        for chat in (OLD_WORKER, CHAT):
            conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, delivery_mode, "
                         "delivery_metadata, created_at, last_event_id) VALUES (?, 'tui', ?, '', 'notify', ?, 1, 0)",
                         (parent, chat, json.dumps({"reason": "create"})))
        conn.commit()
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        assert [s["chat_id"] for s in notify.list_notify_subs(conn, child)] == [CHAT]


def test_b_a_chain_of_only_machine_links_is_unresolved(home):
    """(b) t_01a19451dffa47a8-shaped: every link up the chain is a worker/cron/one-shot transcript -> []."""
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="watch root (created_by a cron)", assignee="default")
        _link(conn, root, CRON_RUN)
        mid = kb.create_task(conn, title="mid", assignee="default", creator_task_id=root)
        _link(conn, mid, ONESHOT)
        _link(conn, mid, CHAT, kind="shepherd", released=1)  # a released shepherd link owns nothing either
        creator = kb.create_task(conn, title="creator", assignee="default", creator_task_id=mid, parents=[root])
        _link(conn, creator, OLD_WORKER)
        assert creator_chats.resolve_chats(conn, creator) == []
        child = kb.create_task(conn, title="child", assignee="default", creator_task_id=creator)
        kinds = [e.kind for e in kb.list_events(conn, child)]
        assert "creator_chats_unresolved" in kinds and "creator_chats_resolved" not in kinds
        assert not conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=? AND released=0", (child,)).fetchall()


@pytest.mark.parametrize("gateway", ["here", "away"])
def test_c_a_cron_id_is_machine_without_a_state_row(home, gateway):
    """(c) ``cron_<job>_<ts>`` is a cron run on any gateway, with no state.db row; a missing ordinary id stays human."""
    with kbc.connect_closing() as conn:
        ancestor = kb.create_task(conn, title="human ancestor", assignee="default")
        creator = kb.create_task(conn, title="cron-owned creator", assignee="default", parents=[ancestor])
        _link(conn, ancestor, CHAT, gateway="away")
        _link(conn, creator, CRON_ID, gateway=gateway)
        assert _sessions(creator_chats.resolve_chats(conn, creator)) == [("away", CHAT)]
    assert notify.is_machine_session(gateway, CRON_ID)
    assert notify.is_machine_session("hub", f"{gateway}/default/{CRON_ID}")
    assert not notify.is_machine_session(gateway, "20261101_000000_nosuch")
    assert not notify.is_machine_session("away", OLD_WORKER)  # another gateway's state.db is not readable here


def test_d_replica_link_released_by_the_chats_gateway_is_skipped(home, monkeypatch):
    """(d) Another gateway (hub) owns an OPEN card linked to this gateway's worker transcript. This gateway's
    reconcile writes that link back released (captured, so it syncs), and the hub's resolver, which cannot read
    our state.db, then skips it and walks on to the human chat."""
    shadow = home / "kanban" / "sync-shadow" / "default.db"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    kbc.init_db(shadow)
    with kbc.connect_closing(shadow) as rep:
        ancestor = kb.create_task(rep, title="hub ancestor", assignee="default")
        card = kb.create_task(rep, title="hub card", assignee="default", parents=[ancestor])
        rep.execute("UPDATE tasks SET created_on='hub', status='running' WHERE id IN (?, ?)", (ancestor, card))
        _link(rep, ancestor, CHAT, gateway="away")
        _link(rep, card, OLD_WORKER)
    uid = links.link_uid(card, "here", "default", OLD_WORKER, "created")
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "here", ledger=False)
        row = conn.execute("SELECT released FROM task_chat_links WHERE uid=?", (uid,)).fetchone()
        assert row is not None and row[0] == 1
        assert conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_chat_links' AND row_uid=? "
                            "AND fields LIKE '%released%'", (uid,)).fetchone()
    # The hub's copy after the sync: same uid, released. On the hub "here" is a foreign gateway.
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: ("hub", {"here", "away", "hub"}))
    with kbc.connect_closing(shadow) as rep:
        rep.execute("UPDATE task_chat_links SET released=1 WHERE uid=?", (uid,))
        rep.commit()
        assert _sessions(creator_chats.resolve_chats(rep, card)) == [("away", CHAT)]


def _cli(release):
    from hermes_cli import kanban_sync_cli
    return kanban_sync_cli.cmd_sync(argparse.Namespace(sync_action="machine-links", board="default", json=True,
                                                       release=release))


def test_e_repair_dry_run_then_release_then_nothing(home, capsys, monkeypatch):
    """(e) Dry run lists the links to local machine sessions; the release releases exactly those (a receipt names
    them); a second run releases 0. Human links stay."""
    from types import SimpleNamespace
    from hermes_cli import kanban_sync_client as ksc
    monkeypatch.setattr(ksc, "load_identity", lambda *a, **k: SimpleNamespace(gateway="here"))
    with kbc.connect_closing() as conn:
        by_worker = kb.create_task(conn, title="filed by an old worker", assignee="default", session_id=OLD_WORKER)
        by_cron = kb.create_task(conn, title="filed by a cron run", assignee="default", session_id=CRON_ID)
        by_human = kb.create_task(conn, title="filed by a human", assignee="default", session_id=CHAT)
        shepherded = kb.create_task(conn, title="watched by a one-shot", assignee="default")
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, delivery_mode, "
                     "delivery_metadata, created_at, last_event_id) VALUES (?, 'tui', ?, '', 'notify', ?, 1, 0)",
                     (shepherded, ONESHOT, json.dumps({"reason": "shepherd"})))
        conn.commit()
        _link(conn, by_human, SUBAGENT)  # an inherited link to a subagent transcript
        for task_id, session, kind, profile in ((by_worker, OLD_WORKER, "created", None),
                                                (by_cron, CRON_ID, "created", None),
                                                (by_human, CHAT, "created", None),
                                                (shepherded, ONESHOT, "shepherd", None)):
            _link(conn, task_id, session, profile=profile, kind=kind)
    expected = sorted([links.link_uid(by_worker, "here", None, OLD_WORKER, "created"),
                       links.link_uid(by_cron, "here", None, CRON_ID, "created"),
                       links.link_uid(shepherded, "here", None, ONESHOT, "shepherd"),
                       links.link_uid(by_human, "here", "default", SUBAGENT, "created")])
    repairs = home / "kanban" / "repairs"

    assert _cli(release=False) == 0
    dry = json.loads(capsys.readouterr().out).get("default", {})
    assert sorted(dry.get("uids", [])) == expected
    with kbc.connect_closing() as conn:  # the dry run wrote nothing
        assert conn.execute("SELECT COUNT(*) FROM task_chat_links WHERE released=1").fetchone()[0] == 0
    assert not repairs.exists() or not list(repairs.iterdir())

    assert _cli(release=True) == 0
    real = json.loads(capsys.readouterr().out)["default"]
    assert (real["released"], real["left"]) == (4, 0)
    with kbc.connect_closing() as conn:
        released = sorted(r[0] for r in conn.execute("SELECT uid FROM task_chat_links WHERE released=1"))
        assert released == expected
        human = links.link_uid(by_human, "here", None, CHAT, "created")
        assert conn.execute("SELECT released FROM task_chat_links WHERE uid=?", (human,)).fetchone()[0] == 0
    receipts = list(repairs.iterdir())
    assert len(receipts) == 1
    assert sorted(r["uid"] for r in json.loads(receipts[0].read_text())["released"]) == expected

    assert _cli(release=True) == 0
    again = json.loads(capsys.readouterr().out).get("default", {})
    assert again.get("pending", 0) == 0 and again.get("released", 0) == 0
    with kbc.connect_closing() as conn:  # the minute reconcile loop keeps them released, writes no new receipt
        links.reconcile(conn, "default", "here", ledger=False)
        assert sorted(r[0] for r in conn.execute("SELECT uid FROM task_chat_links WHERE released=1")) == expected
    assert len(list(repairs.iterdir())) == 1
