"""A chat's cards placed on another gateway reach its task list while its own Board sync is down. [daveey]

Incident (2026-11-13, David-MBP): the gateway gave up the Board sync role (``SchemaAhead``) at 23:53 PT. Chat
20261026_235722_bef6c6 then placed six cards on worker-2/4/7 and the hub. Their done notices arrived (hub.db
``sessions`` links, ``kanban_remote_notices``), but with ``kanban.sync.links_reader: replica`` the chat's task list,
sidebar counts and ask-gate read only Board ``task_chat_links`` rows, and only the chat's own gateway wrote those, from
its origin ledger, through the sync that was down. None of the six cards had a row (t_01a1d44d87ea4bef).

The owner now publishes the link from its ``platform='hub'`` subscription (``kanban_chat_links._owner_hub_links``):
the same uid the chat's gateway writes, insert-only, so the recovered chat gateway upserts that one row.

Two gateways run here against one fake Board, each in its own home: the chat's (macbook) and the owner (worker-7).
The stopped sync is produced the real way (``tests/tools/test_kanban_board_delivery._give_up_the_sync_role``).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_runstate as runstate
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OWNER, BOARD = "macbook", "worker-7", "default"
CHAT = "20261026_235722_bef6c6"


class Gateways:
    """Two gateway homes in one process: ``at(name)`` points HERMES_HOME (and the sync client) at one of them."""

    def __init__(self, tmp_path: Path, monkeypatch, svc: FakeService):
        self.mp, self.svc, self.ledger = monkeypatch, svc, []
        self.homes = {name: tmp_path / name / ".hermes" for name in (ME, OWNER)}
        for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK",
                    "HERMES_KANBAN_RUN_ID", "HERMES_ORIGIN_SESSION", "HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
            monkeypatch.delenv(var, raising=False)
        # The chat gateway's origin ledger (``_remote_links``): what its placement recorded for its chats. The
        # owner's ledger has no rows for macbook chats (``_ledger_links`` keeps only its own origin's rows).
        monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: list(self.ledger))
        self.conns: dict[str, sqlite3.Connection] = {}
        for name, home in self.homes.items():
            home.mkdir(parents=True)
            reader = "    links_reader: replica\n" if name == ME else ""
            (home / "config.yaml").write_text(f"kanban:\n  sync:\n    enabled: true\n{reader}")
            self.at(name)
            conn = kbc.connect(home / "kanban.db")
            ksc.ensure_identity(conn, BOARD, name)
            sync.set_mode(conn, "primary")
            self.conns[name] = conn

    def at(self, name: str) -> Path:
        home = self.homes[name]
        self.mp.setenv("HERMES_HOME", str(home))
        self.mp.setenv("HERMES_KANBAN_HOME", str(home))
        self.mp.setattr(Path, "home", lambda: home.parent)
        self.mp.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://board.test", "k", name))
        ksc.reset_board_watch()
        ksa._reset_cache_for_tests()
        links._reset_for_tests()
        sync.consume_dispatcher_wake()
        return home

    def links_pass(self, name: str) -> None:
        """The sync loop's links pass on ``name`` (``reconcile_links``), as run before each push."""
        home = self.at(name)
        ksc.reconcile_links([(BOARD, home / "kanban.db")], name)

    def round(self, name: str) -> None:
        """One full sync round on ``name``: its links pass, then push the outbox and pull (live + replica)."""
        self.links_pass(name)
        home = self.at(name)
        result = ksc.sync_board(BOARD, home / "kanban.db", self.svc.hub(name), name)
        assert not result.error, result

    def pull_replica(self, name: str) -> None:
        self.at(name)
        ksc._pull_replica(BOARD, self.svc.hub(name), name)

    def replica_rows(self, name: str, task_id: str) -> list[tuple]:
        self.at(name)
        conn = sqlite3.connect(f"file:{ksc.shadow_path(BOARD)}?mode=ro", uri=True)
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT uid, chat_gateway, chat_profile, chat_session, kind, released FROM task_chat_links "
                "WHERE task_id = ? ORDER BY uid", (task_id,))]
        finally:
            conn.close()

    def close(self) -> None:
        for conn in self.conns.values():
            conn.close()


@pytest.fixture
def gws(tmp_path, monkeypatch):
    previous = sync.set_lease_client(support.FakeLeaseClient())
    g = Gateways(tmp_path, monkeypatch, FakeService())
    try:
        yield g
    finally:
        g.close()
        sync.set_lease_client(previous)
        ksa._reset_cache_for_tests()
        links._reset_for_tests()


def _give_up(gws: Gateways) -> None:
    """The chat gateway's sync loop meets a board a newer core migrated and gives up its role (PR #2033's fixture)."""
    from tests.tools.test_kanban_board_delivery import _give_up_the_sync_role
    home = gws.at(ME)
    with pytest.MonkeyPatch.context() as mp:
        _give_up_the_sync_role(home, mp)
    assert runstate.down() and "SchemaAhead" in runstate.down()["reason"]


def _placed(gws: Gateways, title: str = "placed from the chat") -> str:
    """The chat places a card on worker-7: the owner's courier create (``kanban_create`` with an origin intent)."""
    from tools.registry import registry
    import tools.kanban_tools  # registers kanban_create
    gws.at(OWNER)
    intent = {"request_id": f"placement:{ME}:{abs(hash(title)):016x}",
              "owner": {"gateway": OWNER, "profile": "default", "board": BOARD},
              "origin": {"gateway": ME, "profile": "default", "session": CHAT}}
    res = json.loads(registry.dispatch("kanban_create", {"title": title, "assignee": "default"}, origin_intent=intent))
    assert res.get("created") is True, res
    tid = res["task_id"]
    gws.ledger.append({"id": tid, "board": BOARD, "gateway": OWNER, "profile": "default",
                       "key": f"{OWNER}/default/{BOARD}/{tid}", "session": CHAT, "via": "origin",
                       "origin_gateway": ME, "origin_profile": "default", "released": False, "status": "ready",
                       "unfinished": True, "title": title})
    return tid


def _chat_view(gws: Gateways) -> tuple[list[str], int, dict]:
    gws.at(ME)
    tasks = [t["id"] for t in ksa.session_tasks([CHAT])]
    return tasks, ksa.active_task_counts().get(CHAT, 0), links.links_diff(ME)


def test_a_card_placed_while_the_chat_gateways_sync_is_down_shows_in_its_task_list(gws):
    _give_up(gws)
    tid = _placed(gws)
    gws.round(OWNER)  # the owner's sync runs: its links pass publishes the chat's link with the card
    gws.pull_replica(ME)  # one replica pull on the chat's gateway
    assert runstate.down()  # still stopped: nothing of the chat gateway's own reached the Board
    tasks, count, diff = _chat_view(gws)
    # Before the fix: [] and 0, and links_diff listed the card only in the ledger.
    assert tasks == [tid] and count == 1
    assert diff["clean"], diff
    rows = gws.replica_rows(ME, tid)
    assert rows == [(links.link_uid(tid, ME, "default", CHAT, "created"), ME, "default", CHAT, "created", 0)]


def test_the_recovered_chat_gateway_upserts_the_same_row_never_a_second(gws):
    _give_up(gws)
    tid = _placed(gws)
    gws.round(OWNER)
    gws.pull_replica(ME)
    runstate.clear()  # a fixed core restarts the loop: the chat gateway's links pass and push run again
    gws.round(ME)
    gws.round(OWNER)
    gws.round(ME)
    rows = gws.replica_rows(ME, tid)
    assert [r for r in rows if r[5] == 0] == [
        (links.link_uid(tid, ME, "default", CHAT, "created"), ME, "default", CHAT, "created", 0)]
    assert len(rows) == 1
    tasks, count, diff = _chat_view(gws)
    assert tasks == [tid] and count == 1
    assert diff["clean"], diff
    # The chat gateway wrote the uid itself, so it owns the row now; the owner's passes add nothing more.
    assert gws.conns[ME].execute("SELECT COUNT(*) FROM task_chat_links WHERE task_id = ?", (tid,)).fetchone()[0] == 1


def test_a_card_finished_during_the_outage_is_released_by_the_recovered_chat_gateway(gws):
    _give_up(gws)
    tid = _placed(gws)
    gws.round(OWNER)
    gws.at(OWNER)
    assert kb.complete_task(gws.conns[OWNER], tid, result="done")
    gws.round(OWNER)
    # The chat gateway's ledger released the obligation (verified done), so its projection no longer lists it.
    gws.ledger.clear()
    runstate.clear()
    gws.pull_replica(ME)  # the replica shows the card done
    gws.round(ME)
    gws.pull_replica(ME)
    assert [r[5] for r in gws.replica_rows(ME, tid)] == [1]
    tasks, count, diff = _chat_view(gws)
    assert tasks == [] and count == 0 and diff["clean"], diff


def _sub(conn, tid, chat_id, reason=None):
    meta = json.dumps({"reason": reason}) if reason else None
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, delivery_metadata, "
                     "created_at) VALUES (?, 'hub', ?, '', ?, 5)", (tid, chat_id, meta))


def _owner_links(conn, tid):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT chat_gateway, chat_profile, chat_session, kind, released FROM task_chat_links WHERE task_id = ?",
        (tid,)))


def test_owner_publishes_counted_hub_subscriptions_of_open_cards_only(gws):
    gws.at(OWNER)
    conn = gws.conns[OWNER]
    tid = kb.create_task(conn, title="watched", assignee="default")
    done = kb.create_task(conn, title="finished", assignee="default")
    _sub(conn, tid, "hub/alice/chat-shepherd", "shepherd")          # a cross_task shepherd on another gateway
    _sub(conn, tid, "mini/default/chat-touch", "touch")             # notice-only: never the chat's work
    _sub(conn, tid, "mini/default/chat-refused", "send_refused")
    _sub(conn, tid, f"{OWNER}/default/own-chat")                     # this gateway's own chat: its tui subs say it
    _sub(conn, tid, "mini/default/kanban-placement-mini")            # a placement attribution id, never a chat
    _sub(conn, tid, "mini/default/local-" + "7c" * 32)                # a worker receipt hash, never a chat
    _sub(conn, tid, "not a chat ref")
    _sub(conn, done, "hub/default/chat-late")
    assert kb.complete_task(conn, done, result="ok")
    out = links.reconcile(conn, BOARD, OWNER)
    assert _owner_links(conn, tid) == [("hub", "alice", "chat-shepherd", "shepherd", 0)]
    assert _owner_links(conn, done) == []
    assert out["added"] >= 1
    # Idempotent: a second pass writes nothing and queues nothing.
    queued = conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'task_chat_links'").fetchone()[0]
    assert links.reconcile(conn, BOARD, OWNER)["added"] == 0
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'task_chat_links'"
                        ).fetchone()[0] == queued


def test_owner_never_reopens_or_doubles_a_link_the_chat_gateway_already_wrote(gws):
    gws.at(OWNER)
    conn = gws.conns[OWNER]
    tid = kb.create_task(conn, title="watched", assignee="default")
    _sub(conn, tid, "hub/default/chat-a", "shepherd")
    _sub(conn, tid, "hub/default/chat-b", "shepherd")
    # chat-a: the chat's gateway already released its link (same card and chat, either kind); chat-b: it wrote the
    # ``created`` kind for it. Both reached the owner's replica only.
    rep = ksc.shadow_path(BOARD)
    replica = ksc.open_shadow(BOARD, OWNER)
    replica.close()
    raw = sqlite3.connect(rep)
    raw.executemany("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                    "released, created_at) VALUES (?, ?, 'hub', 'default', ?, ?, ?, 1)",
                    [(links.link_uid(tid, "hub", "default", "chat-a", "shepherd"), tid, "chat-a", "shepherd", 1),
                     (links.link_uid(tid, "hub", "default", "chat-b", "created"), tid, "chat-b", "created", 0)])
    raw.commit()
    raw.close()
    links.reconcile(conn, BOARD, OWNER)
    assert _owner_links(conn, tid) == []
    # A replica that cannot be read writes nothing this pass (never a guess over a release it could not see).
    _sub(conn, tid, "hub/default/chat-c", "shepherd")
    rep.write_bytes(b"not a database")
    links.reconcile(conn, BOARD, OWNER)
    assert _owner_links(conn, tid) == []


# --- PR #2095 review follow-ups (t_01a1d480e6d8fd12) -------------------------------------------------------------

TIP = "20261027_081500_c0ffee"


def _compress(gws: Gateways, old: str, new: str) -> None:
    """The chat compresses on its own gateway: ``old`` ends ``compression`` and ``new`` continues it."""
    from hermes_state import SessionDB
    home = gws.at(ME)
    state = SessionDB(db_path=home / "state.db")
    try:
        if state.get_session(old) is None:
            state.create_session(old, "cli")
        state.end_session(old, "compression")
        state.create_session(new, "cli", parent_session_id=old)
    finally:
        state.close()


def test_a_chat_compressed_during_the_outage_keeps_one_unreleased_row_and_a_clean_diff(gws):
    """The hub sub names the session that placed the card (S0); the chat compressed before its gateway accepted the
    binding, so that gateway's ledger resolves the tip (S1). Before the fix both rows stayed unreleased and
    links_diff listed S0 only_in_replica until the card closed."""
    _give_up(gws)
    tid = _placed(gws)
    _compress(gws, CHAT, TIP)
    gws.ledger[0]["session"] = TIP  # the recovered gateway's ledger resolves the chat's tip
    gws.round(OWNER)  # the owner publishes S0's link from its hub sub
    gws.pull_replica(ME)
    runstate.clear()
    gws.round(ME)  # the chat gateway writes S1's link and releases the owner's S0 row
    gws.round(OWNER)
    gws.round(ME)
    rows = gws.replica_rows(ME, tid)
    assert [r for r in rows if r[5] == 0] == [
        (links.link_uid(tid, ME, "default", TIP, "created"), ME, "default", TIP, "created", 0)]
    assert (links.link_uid(tid, ME, "default", CHAT, "created"), ME, "default", CHAT, "created", 1) in rows
    gws.at(ME)
    diff = links.links_diff(ME)
    assert diff["clean"], diff
    assert [t["id"] for t in ksa.session_tasks([TIP])] == [tid]
    # The owner never writes S0's link back: a released row for that card and chat is still a row.
    gws.at(OWNER)
    assert links.reconcile(gws.conns[OWNER], BOARD, OWNER)["added"] == 0


def test_an_uncompressed_second_chat_on_the_same_card_keeps_its_owner_row(gws):
    """Only a compression ancestor is released: another chat's (or a sibling branch's) owner-written link stays."""
    _give_up(gws)
    tid = _placed(gws)
    other = "20261027_090000_beef00"
    from hermes_state import SessionDB
    state = SessionDB(db_path=gws.at(ME) / "state.db")
    try:
        state.create_session(other, "cli")
    finally:
        state.close()
    gws.at(OWNER)
    _sub(gws.conns[OWNER], tid, f"{ME}/default/{other}", "shepherd")
    gws.round(OWNER)
    gws.pull_replica(ME)
    runstate.clear()
    gws.round(ME)
    gws.round(ME)
    unreleased = sorted(r[3] for r in gws.replica_rows(ME, tid) if r[5] == 0)
    assert unreleased == sorted([CHAT, other])


def test_owner_skips_a_cron_session_the_chat_gateway_would_release(gws):
    """The owner filtered worker hashes but not ``cron_`` sessions, so a cron chat's hub sub got a row the chat
    gateway (``machine_sessions``) released on its next pass."""
    from hermes_cli.kanban_db_notify import machine_sessions
    cron = "cron_3f2a1b_20261113_101500"
    assert machine_sessions("mini", [cron], local_gateway="mini") == {cron}  # the chat gateway's own verdict
    gws.at(OWNER)
    conn = gws.conns[OWNER]
    tid = kb.create_task(conn, title="watched by a cron chat", assignee="default")
    _sub(conn, tid, f"mini/default/{cron}", "shepherd")
    _sub(conn, tid, "mini/default/chat-human", "shepherd")
    links.reconcile(conn, BOARD, OWNER)
    assert _owner_links(conn, tid) == [("mini", "default", "chat-human", "shepherd", 0)]


def _plan_detail(conn, sql, args):
    return " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_task_chat_links_has_a_task_id_index_in_board_files_and_replicas_and_migrates_old_files(gws, tmp_path):
    owner_lookup = ("SELECT task_id, chat_gateway, COALESCE(chat_profile, 'default'), chat_session "
                    "FROM task_chat_links WHERE task_id IN (?, ?)")
    gws.at(OWNER)
    board = gws.conns[OWNER]
    replica = ksc.open_shadow(BOARD, OWNER)
    try:
        for conn in (board, replica):
            assert "USING INDEX idx_task_chat_links_task" in _plan_detail(conn, owner_lookup, ("a", "b"))
    finally:
        replica.close()
    # A board file synced by a core that predates the index gets it on the next process's first open.
    path = gws.homes[OWNER] / "kanban.db"
    with kbc.write_txn(board):
        board.execute("DROP INDEX idx_task_chat_links_task")
    kbc._INITIALIZED_PATHS.clear()
    reopened = kbc.connect(path)
    try:
        names = {r[0] for r in reopened.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'task_chat_links'")}
        assert "idx_task_chat_links_task" in names
        # An index is not a column: the schema guard still finds nothing this core does not declare.
        assert not ksc.schema_ahead(reopened)
    finally:
        reopened.close()


def test_archived_card_publishes_no_owner_link_for_its_leftover_hub_sub_and_keeps_the_open_card_link(gws):
    """A hub sub left on an archived card (the chat that placed it is long gone) writes no link, so it cannot keep
    that chat waiting; an unrelated open card's sub for the same chat is untouched. [daveey]"""
    gws.at(OWNER)
    conn = gws.conns[OWNER]
    archived = kb.create_task(conn, title="withdrawn", assignee="default")
    open_card = kb.create_task(conn, title="still going", assignee="default")
    _sub(conn, archived, "hub/default/chat-same", "shepherd")
    _sub(conn, open_card, "hub/default/chat-same", "shepherd")
    assert kb.archive_task(conn, archived)
    links.reconcile(conn, BOARD, OWNER)
    assert _owner_links(conn, archived) == []
    assert _owner_links(conn, open_card) == [("hub", "default", "chat-same", "shepherd", 0)]
    assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (open_card,)).fetchone()[0] == 1
