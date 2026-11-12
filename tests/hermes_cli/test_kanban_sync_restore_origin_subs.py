"""A board file restored from the Board gets back the hub delivery subscriptions its chat links stand for.

A worker reprovisioned from the Board (worker-1, 2026-11-11) got its cards and their ``task_chat_links`` back,
but ``kanban_notify_subs`` is machine-local and does not sync. The hermes-hub collector builds a card's
``sessions`` (and the notifier its deliveries) only from those subscriptions, so the restored owner republished
100 of 215 cards with ``sessions: []`` and stopped notifying their origin chats (t_01a1cf4cfd7eb125). [daveey]
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_chat_links import link_uid
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "worker-1", "macbook", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


def _open(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    return conn


@pytest.fixture
def live(kanban_home, svc):
    conn = _open(kanban_home, svc)
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
        conn.close()


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


def _reprovision(kanban_home, svc, live):
    """A fresh machine with the same gateway name: no board file, no replica; the Board holds everything."""
    live.close()
    ksc.reset_board_watch()
    for path in kanban_home.rglob("*.db*"):
        path.unlink()
    for path in (kanban_home / "kanban" / sync.SHADOW_DIR).glob("*") if (kanban_home / "kanban").exists() else ():
        path.unlink()
    conn = _open(kanban_home, svc)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    _round(kanban_home, svc)
    return conn


def _hub_subs(conn, task_id):
    return sorted((r[0], json.loads(r[1] or "{}").get("reason"))
                  for r in conn.execute("SELECT chat_id, delivery_metadata FROM kanban_notify_subs "
                                        "WHERE task_id = ? AND platform = 'hub'", (task_id,)))


def _collector_sessions(conn, task_id):
    """What the hermes-hub collector publishes for a card's ``sessions`` from its subscriptions (``hermes_hub.py``
    ``collect_tasks``: platform 'hub' rows, ``<gw>/<profile>/<session>``, uncounted reasons excluded)."""
    out = []
    for (chat_id,) in conn.execute(
            "SELECT chat_id FROM kanban_notify_subs WHERE task_id = ? AND platform = 'hub' AND CASE WHEN "
            "json_valid(delivery_metadata) THEN COALESCE(json_extract(delivery_metadata, '$.reason'), '') "
            "NOT IN ('touch', 'send_refused') ELSE 1 END ORDER BY chat_id", (task_id,)):
        gw, profile, session = chat_id.split("/")
        out.append({"gw": gw, "profile": profile, "session": session, "rel": "shepherd"})
    return out


def test_a_card_restored_from_the_board_publishes_its_origin_chat_again(live, svc, kanban_home):
    tid = kb.create_task(live, title="origin card", assignee="worker", chat_origin="hub/default/chat-a")
    assert _hub_subs(live, tid) == [("hub/default/chat-a", "origin")]
    _round(kanban_home, svc)

    restored = _reprovision(kanban_home, svc, live)
    try:
        assert kb.get_task(restored, tid) is not None
        assert [tuple(r) for r in restored.execute(
            "SELECT chat_gateway, chat_profile, chat_session, released FROM task_chat_links WHERE task_id = ?",
            (tid,))] == [("hub", "default", "chat-a", 0)]
        # Before the fix: [] (the subscription stayed on the old machine) and the collector published sessions=[].
        assert _hub_subs(restored, tid) == [("hub/default/chat-a", "origin")]
        assert _collector_sessions(restored, tid) == [
            {"gw": "hub", "profile": "default", "session": "chat-a", "rel": "shepherd"}]
        # Caught up: the restored subscription never replays the card's history into the chat.
        assert restored.execute(
            "SELECT last_event_id = (SELECT MAX(id) FROM task_events WHERE task_id = ?) FROM kanban_notify_subs "
            "WHERE task_id = ?", (tid, tid)).fetchone()[0] == 1
        _round(kanban_home, svc)  # idempotent: a second round writes nothing more
        assert _hub_subs(restored, tid) == [("hub/default/chat-a", "origin")]
    finally:
        restored.close()


def test_restore_keeps_exact_identities_and_skips_released_null_profile_and_local_chats(live, svc, kanban_home):
    tid = kb.create_task(live, title="card", assignee="worker", chat_origin="hub/alice/chat-a")
    rows = [  # (gateway, profile, session, kind, released)
        ("hub", "alice", "chat-a", "shepherd", 0),     # same chat as the origin: one subscription, owning wins
        (OTHER, "default", "chat-b", "shepherd", 0),   # an observer on another gateway
        (OTHER, None, "chat-null", "created", 0),      # NULL profile: no exact chat, never a wildcard
        (OTHER, "default", "chat-gone", "shepherd", 1),  # released: the chat no longer has the card
        (ME, None, "local-chat", "shepherd", 0),       # this gateway's own chat is no hub destination
    ]
    with kbc.write_txn(live):
        for gw, profile, session, kind, released in rows:
            live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                         "released, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                         (link_uid(tid, gw, profile, session, kind), tid, gw, profile, session, kind, released))
    _round(kanban_home, svc)

    restored = _reprovision(kanban_home, svc, live)
    try:
        assert restored.execute("SELECT COUNT(*) FROM task_chat_links WHERE task_id = ?", (tid,)).fetchone()[0] == 6
        assert _hub_subs(restored, tid) == [("hub/alice/chat-a", "origin"), (f"{OTHER}/default/chat-b", "shepherd")]
        sessions = _collector_sessions(restored, tid)
        assert len(sessions) == len({(s["gw"], s["profile"], s["session"]) for s in sessions})  # no duplicates
        assert all(s["profile"] not in (None, "", "None") for s in sessions)
        assert not restored.execute("SELECT 1 FROM kanban_notify_subs WHERE task_id = ? AND platform = 'tui'",
                                    (tid,)).fetchone()
    finally:
        restored.close()


def test_a_link_arriving_for_an_existing_card_does_not_resurrect_a_removed_subscription(live, svc, kanban_home):
    tid = kb.create_task(live, title="card", assignee="worker", chat_origin="hub/default/chat-a")
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_notify_subs WHERE task_id = ? AND platform = 'hub'", (tid,))
    # Another gateway re-sends the same link row (its reconcile), and a second link for the card.
    hub = svc.hub(OTHER)
    stamp = f"{time.time_ns() // 1_000_000 + 1000:013d}-0000-{OTHER}"
    for n, (session, kind) in enumerate((("chat-a", "created"), ("chat-c", "shepherd"))):
        uid = link_uid(tid, "hub", "default", session, kind)
        res = hub.ops(BOARD, [{"client_op_id": f"other-{n}", "op": "upsert", "table": "task_chat_links", "uid": uid,
                               "hlc": stamp, "fields": {"task_id": tid, "chat_gateway": "hub", "chat_profile": "default",
                                                        "chat_session": session, "kind": kind, "released": 0,
                                                        "created_at": 2}}])
        assert res["results"][0]["status"] == "applied", res
    _round(kanban_home, svc)
    # The existing row is not a restore. A new link for a card this file already held is not either: only a
    # card materialized from the Board in the same apply gets its subscriptions rebuilt.
    assert _hub_subs(live, tid) == []


def test_reconcile_after_a_rebuild_that_lost_the_card_restores_its_subscription(live, svc, kanban_home):
    tid = kb.create_task(live, title="card", assignee="worker", chat_origin="hub/default/chat-a")
    _round(kanban_home, svc)
    # A corruption rebuild lost the card, its links and its subscription without capture (no outbox, no delete).
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        for table in ("kanban_notify_subs", "task_chat_links", "task_events", "task_runs"):
            live.execute(f"DELETE FROM {table} WHERE task_id = ?", (tid,))
        live.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    report = ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))
    assert report["restored"].get("tasks") == 1 and report["restored"].get("task_chat_links") == 1
    assert _hub_subs(live, tid) == [("hub/default/chat-a", "origin")]


def _lose(live, tid):
    """A corruption rebuild loses the card, its links and its subscriptions without capture."""
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        for table in ("kanban_notify_subs", "task_chat_links", "task_events", "task_runs"):
            live.execute(f"DELETE FROM {table} WHERE task_id = ?", (tid,))
        live.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        live.execute("UPDATE kanban_sync_state SET applying = 0")


def test_a_second_loss_of_the_same_card_restores_links_written_after_the_first_restore(live, svc, kanban_home):
    tid = kb.create_task(live, title="card", assignee="worker", chat_origin="hub/default/chat-a")
    _round(kanban_home, svc)
    _lose(live, tid)
    ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))
    assert _hub_subs(live, tid) == [("hub/default/chat-a", "origin")]
    # An hour later a chat on another gateway starts shepherding the card.
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_restored_cards SET restored_at = restored_at - 3600 WHERE task_id = ?", (tid,))
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, ?, ?, 'default', 'chat-new', 'shepherd', 0, ?)",
                     (link_uid(tid, OTHER, "default", "chat-new", "shepherd"), tid, OTHER, int(time.time())))
    _round(kanban_home, svc)
    # The card is lost again: the newest restore covers the newer link too.
    _lose(live, tid)
    report = ksc.reconcile(BOARD, kanban_home / "kanban.db", hub=svc.hub(ME))
    assert report["restored"].get("tasks") == 1
    assert _hub_subs(live, tid) == [("hub/default/chat-a", "origin"), (f"{OTHER}/default/chat-new", "shepherd")]


def test_the_replica_copy_and_foreign_cards_get_no_subscriptions(live, svc, kanban_home):
    tid = kb.create_task(live, title="card", assignee="worker", chat_origin="hub/default/chat-a")
    hub = svc.hub(OTHER)
    stamp = f"{time.time_ns() // 1_000_000 + 1000:013d}-0000-{OTHER}"
    hub.ops(BOARD, [
        {"client_op_id": "f-card", "op": "upsert", "table": "tasks", "uid": "t_foreign", "hlc": stamp,
         "fields": {"title": "theirs", "status": "running", "created_at": 1, "created_on": OTHER}},
        {"client_op_id": "f-link", "op": "upsert", "table": "task_chat_links",
         "uid": link_uid("t_foreign", "hub", "default", "chat-f", "created"), "hlc": stamp,
         "fields": {"task_id": "t_foreign", "chat_gateway": "hub", "chat_profile": "default", "chat_session": "chat-f",
                    "kind": "created", "released": 0, "created_at": 1}}])
    _round(kanban_home, svc)
    replica = ksc.open_shadow(BOARD, ME)
    try:
        assert replica.execute("SELECT COUNT(*) FROM task_chat_links WHERE task_id = 't_foreign'").fetchone()[0] == 1
        assert replica.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0] == 0
    finally:
        replica.close()
    restored = _reprovision(kanban_home, svc, live)
    try:
        assert kb.get_task(restored, "t_foreign") is None
        assert not restored.execute("SELECT 1 FROM kanban_notify_subs WHERE task_id = 't_foreign'").fetchone()
        assert _hub_subs(restored, tid) == [("hub/default/chat-a", "origin")]
        replica = ksc.open_shadow(BOARD, ME)
        try:
            assert replica.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0] == 0
        finally:
            replica.close()
    finally:
        restored.close()
