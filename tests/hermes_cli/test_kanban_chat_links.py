"""Chat links synced through the Board (RFC phase 5): reconcile writes them from local data and the origin ledger,
the replica answers a chat's remote cards, links-diff compares the two answers, session_tasks switches on config."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

ME, OTHER = "macbook", "hub"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, "default", ME)
    ksa._reset_cache_for_tests()
    try:
        yield conn
    finally:
        conn.close()
        ksa._reset_cache_for_tests()


def _links(conn):
    return {(r[0], r[1], r[2], r[3]) for r in conn.execute(
        "SELECT task_id, chat_session, kind, released FROM task_chat_links ORDER BY task_id")}


def _ledger_row(task_id, session, *, via="shepherd", released=False, status="running", board="default"):
    return {"id": task_id, "board": board, "gateway": OTHER, "key": f"{OTHER}/default/{board}/{task_id}",
            "session": session, "via": via, "origin_gateway": ME, "origin_profile": "default", "released": released,
            "status": status, "unfinished": not released, "title": task_id}


def test_reconcile_writes_created_and_shepherd_links_once_and_through_capture(live, monkeypatch):
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    mine = kb.create_task(live, title="mine", assignee="worker", session_id="s_creator")
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'tui', 's_watcher', 1)", (mine,))
        live.execute("DELETE FROM kanban_sync_outbox")
    assert links.reconcile(live, "default", ME) == {"added": 2, "changed": 0, "released": 0, "retired": 0}
    assert _links(live) == {(mine, "s_creator", "created", 0), (mine, "s_watcher", "shepherd", 0)}
    queued = [r[0] for r in live.execute("SELECT table_name FROM kanban_sync_outbox")]
    assert queued.count("task_chat_links") == 2  # pushed like any synced row
    assert links.reconcile(live, "default", ME) == {"added": 0, "changed": 0, "released": 0, "retired": 0}  # idempotent
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_notify_subs")
    assert links.reconcile(live, "default", ME)["released"] == 1  # an unsubscribe releases, never deletes
    assert (mine, "s_watcher", "shepherd", 1) in _links(live)


WORKER = "local-" + "7c" * 32  # a Kanban worker / cron receipt session: never a chat


def test_a_worker_transcript_gets_no_shepherd_link_and_an_old_one_is_retired(live, monkeypatch):
    """Hub card t_a989e5e0: worker transcripts held shepherd subs/links, so its notice and wake went to sessions that
    refuse them (4132). A worker's shepherd link is never written and an existing one is deleted; a worker's
    ``created`` link (which run made the card) stays."""
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_ledger_row("t_remote", WORKER)])
    mine = kb.create_task(live, title="mine", assignee="worker", session_id=WORKER)
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'tui', ?, 1)",
                     (mine, WORKER))  # an older process wrote it before add_notify_sub refused workers
        live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'tui', 's_chat', 1)",
                     (mine,))
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, "
                     "created_at) VALUES (?, 't_old', ?, NULL, ?, 'shepherd', 0, 1)",
                     (links.link_uid("t_old", ME, None, WORKER, "shepherd"), ME, WORKER))
    out = links.reconcile(live, "default", ME)
    assert out["retired"] == 1
    assert _links(live) == {(mine, WORKER, "created", 0), (mine, "s_chat", "shepherd", 0)}


def test_reconcile_carries_this_gateways_ledger_links_and_their_release(live, monkeypatch):
    rows = [_ledger_row("t_remote1", "s_chat"), _ledger_row("t_remote2", "s_chat", via="origin"),
            _ledger_row("t_other_board", "s_chat", board="runners")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    links.reconcile(live, "default", ME)
    assert _links(live) == {("t_remote1", "s_chat", "shepherd", 0), ("t_remote2", "s_chat", "created", 0)}
    rows[0] = _ledger_row("t_remote1", "s_chat", released=True, status="done")
    assert links.reconcile(live, "default", ME)["changed"] == 1
    assert ("t_remote1", "s_chat", "shepherd", 1) in _links(live)


def _replica(path, tasks, chat_links):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
        "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, chat_session TEXT, "
        "kind TEXT, released INTEGER, created_at INTEGER);")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, 'worker', 1, NULL, 0, NULL, NULL, ?)", tasks)
    conn.executemany("INSERT INTO task_chat_links VALUES (?, ?, ?, 'default', ?, ?, ?, 1)",
                     [(links.link_uid(t, g, "default", s, k), t, g, s, k, r) for t, g, s, k, r in chat_links])
    conn.commit()
    conn.close()


def test_replica_answers_a_chats_open_cards_on_other_gateways(tmp_path):
    rep = tmp_path / "default.db"
    _replica(rep, [("t_open", "open card", "running", OTHER), ("t_done", "done card", "done", OTHER),
                   ("t_mine", "local card", "running", ME), ("t_released", "released", "ready", OTHER),
                   ("t_theirs", "other gateway's chat", "ready", OTHER)],
             [("t_open", ME, "s1", "shepherd", 0), ("t_done", ME, "s1", "shepherd", 0), ("t_mine", ME, "s1", "created", 0),
              ("t_released", ME, "s1", "shepherd", 1), ("t_theirs", "mini", "s1", "shepherd", 0)])
    got = links.replica_cards(["s1", "s0"], ME, replicas=[("default", rep)])
    # A done card whose link is not released is still the chat's (unknown, unfinished): the sidebar counts it, so the
    # list must too. Only a released link drops a card (t_released).
    assert sorted((c["id"], c["gateway"], c["board"], c["via"], c["session_id"], c["status"]) for c in got) == [
        ("t_done", OTHER, "default", "shepherd", "s1", "unknown"), ("t_open", OTHER, "default", "shepherd", "s1", "running")]
    got = {c["id"]: c for c in got}
    assert got["t_open"]["title"] == "open card" and got["t_open"]["source"] == "replica"
    assert got["t_done"]["reported_status"] == "done" and got["t_done"]["unfinished"] is True


def test_links_diff_is_clean_when_both_answers_match_and_lists_the_difference(kanban_home, monkeypatch, tmp_path):
    rep = tmp_path / "default.db"
    _replica(rep, [("t_a", "a", "running", OTHER), ("t_b", "b", "ready", OTHER)],
             [("t_a", ME, "s1", "shepherd", 0), ("t_b", ME, "s1", "shepherd", 0)])
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_ledger_row("t_a", "s1"), _ledger_row("t_b", "s1", status="ready")])
    assert links.links_diff(ME)["clean"]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_ledger_row("t_a", "s1"), _ledger_row("t_c", "s1", board="runners")])
    report = links.links_diff(ME)
    assert not report["clean"]
    assert report["only_in_ledger"] == {"s1": [(OTHER, "runners", "t_c")]}
    assert report["only_in_replica"] == {"s1": [(OTHER, "default", "t_b")]}


def test_session_tasks_reads_remote_cards_from_the_replica_when_switched(live, kanban_home, monkeypatch, tmp_path):
    rep = tmp_path / "default.db"
    _replica(rep, [("t_remote", "from replica", "running", OTHER)], [("t_remote", ME, "s1", "shepherd", 0)])
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_ledger_row("t_ledger", "s1")])
    assert [t["id"] for t in ksa.session_tasks(["s1"])] == ["t_ledger"]  # default: the origin ledger
    (kanban_home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    links_reader: replica\n")
    assert [t["id"] for t in ksa.session_tasks(["s1"])] == ["t_remote"]


def test_the_sync_loop_reconciles_every_synced_board(live, kanban_home, monkeypatch):
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_ledger_row("t_remote", "s_chat")])
    kb.create_task(live, title="x", assignee="worker", session_id="s_local")
    ksc.reconcile_links([("default", kanban_home / "kanban.db")], ME)
    assert {r[1] for r in _links(live)} == {"s_local", "s_chat"}


def test_cli_links_and_links_diff(live, kanban_home, monkeypatch, capsys, tmp_path):
    import argparse
    from hermes_cli import kanban as kc
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    kb.create_task(live, title="x", assignee="worker", session_id="s_local")

    def cli(*argv):
        parser = argparse.ArgumentParser()
        kc.build_parser(parser.add_subparsers(dest="cmd"))
        return kc.kanban_command(parser.parse_args(["kanban", *argv]))

    assert cli("sync", "links") == 0
    assert any(r[1] == "s_local" for r in _links(live))
    capsys.readouterr()
    assert cli("sync", "links-diff", "--json") == 0  # no remote links on either side
    assert json.loads(capsys.readouterr().out)["clean"] is True


def test_replica_keeps_every_chat_of_a_card_shared_by_several(tmp_path):
    rep = tmp_path / "default.db"
    _replica(rep, [("t_shared", "shared", "running", OTHER)],
             [("t_shared", ME, "s1", "shepherd", 0), ("t_shared", ME, "s2", "shepherd", 0)])
    got = links.replica_cards(["s1", "s2"], ME, replicas=[("default", rep)])
    assert sorted(c["session"] for c in got) == ["s1", "s2"]


def test_a_board_only_another_gateway_has_gets_a_link_only_file(live, kanban_home, monkeypatch):
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_ledger_row("t_run", "s_chat", board="runners")])
    ksc.reconcile_links(ksc.board_paths(), ME)
    boards = dict(ksc.board_paths())
    assert boards["runners"] == kanban_home / "kanban" / sync.LINKS_DIR / "runners.db"
    conn = kbc.connect(boards["runners"])
    try:
        assert _links(conn) == {("t_run", "s_chat", "shepherd", 0)}
        assert sync.sync_state(conn)["board_id"] == "runners" and sync.board_mode(conn) == "shadow"
        assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'task_chat_links'").fetchone()[0] == 1
    finally:
        conn.close()
    from hermes_cli import kanban_db
    assert "runners" not in {b.get("slug") for b in kanban_db.list_boards()}  # never a board in the UI


def test_replica_reads_stay_fresh_through_the_shared_connection(tmp_path):
    ksa._reset_cache_for_tests()
    rep = tmp_path / "default.db"
    _replica(rep, [("t_a", "a", "running", OTHER), ("t_b", "b", "ready", OTHER)], [("t_a", ME, "s1", "shepherd", 0)])
    assert [c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])] == ["t_a"]
    conn = sqlite3.connect(rep)
    conn.execute("INSERT INTO task_chat_links VALUES (?, 't_b', ?, 'default', 's1', 'shepherd', 0, 2)",
                 (links.link_uid("t_b", ME, "default", "s1", "shepherd"), ME))
    conn.execute("UPDATE task_chat_links SET released = 1 WHERE task_id = 't_a'")
    conn.commit()
    conn.close()
    assert [c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])] == ["t_b"]
    ksa._reset_cache_for_tests()


_ASK = {"action": "Reply with the runner host", "why_human": "approval-gated", "interaction": "reply",
        "scope": "one runner", "evidence": "runners API shows none"}


def _switch_to_replica(kanban_home, monkeypatch, rep):
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    (kanban_home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    links_reader: replica\n")
    ksa._reset_cache_for_tests()


def test_replica_human_ask_paints_the_chat_blocked(live, kanban_home, monkeypatch, tmp_path):
    """[daveey] The replica stores ``human_request`` as JSON text. Passed through undecoded, ``needs_human`` saw a
    string, so a remote card blocked on David counted as open work: the chat painted waiting, not red, and two
    runner approvals sat unanswered ~18 h. Decoded, it counts blocked; a non-human block stays open work."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_ask", "ask", "blocked", OTHER), ("t_ops", "ops", "blocked", OTHER),
                   ("t_kind", "time wait", "blocked", OTHER), ("t_junk", "junk", "blocked", OTHER)],
             [("t_ask", ME, "s1", "shepherd", 0), ("t_ops", ME, "s2", "shepherd", 0),
              ("t_kind", ME, "s3", "shepherd", 0), ("t_junk", ME, "s4", "shepherd", 0)])
    conn = sqlite3.connect(rep)
    conn.execute("UPDATE tasks SET human_request = ? WHERE id IN ('t_ask', 't_kind')", (json.dumps(_ASK),))
    conn.execute("UPDATE tasks SET block_kind = 'time' WHERE id = 't_kind'")
    conn.execute("UPDATE tasks SET human_request = '{not json' WHERE id = 't_junk'")
    conn.commit()
    conn.close()
    got = {c["id"]: c for c in links.replica_cards(None, ME, ksa.LISTED_STATUSES, replicas=[("default", rep)])}
    assert got["t_ask"]["human_request"] == _ASK  # same shape as the ledger rows
    assert got["t_ops"]["human_request"] is None and got["t_junk"]["human_request"] is None
    _switch_to_replica(kanban_home, monkeypatch, rep)
    assert ksa.blocked_task_counts() == {"s1": 1}
    active = ksa.active_task_counts()
    assert active.get("s1") is None and active["s2"] == 1 and active["s3"] == 1 and active["s4"] == 1
    # ``_replica_bucket`` itself tolerates JSON text from any other reader.
    assert ksa._replica_bucket({"status": "blocked", "human_request": json.dumps(_ASK)}) == "blocked"


def test_unknown_replica_card_waits_like_a_parked_one(live, kanban_home, monkeypatch, tmp_path):
    """[daveey] A done card whose link was never released is ``unknown``: nobody works it, so it is parked (the chat
    paints waiting), not live (which left the row with no stripe at all)."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_done", "done", "done", OTHER), ("t_sched", "later", "scheduled", OTHER),
                   ("t_run", "running", "running", OTHER)],
             [("t_done", ME, "s1", "shepherd", 0), ("t_sched", ME, "s1", "shepherd", 0),
              ("t_run", ME, "s2", "shepherd", 0)])
    _switch_to_replica(kanban_home, monkeypatch, rep)
    assert ksa.active_task_counts() == {"s1": 2, "s2": 1}
    parked, held = ksa.parked_task_counts()
    assert parked == {"s1": 2} and held == {}


def test_session_tasks_stale_ok_reaches_the_replica_owner_labels(live, kanban_home, monkeypatch, tmp_path):
    """[daveey] The Desktop strip's ``stale_ok`` must reach ``_ledger_owners``: it used to stop at
    ``replica_cards``, so ``/api/session-kanban-tasks`` waited on every ledger rebuild in replica mode."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_remote", "from replica", "running", OTHER)], [("t_remote", ME, "s1", "shepherd", 0)])
    _switch_to_replica(kanban_home, monkeypatch, rep)
    calls = []

    def remote_links(stale_ok=False):
        calls.append(stale_ok)
        return [_ledger_row("t_remote", "s1")]

    monkeypatch.setattr(ksa, "_remote_links", remote_links)
    assert [t["id"] for t in ksa.session_tasks(["s1"], stale_ok=True)] == ["t_remote"]
    assert calls == [True]
    calls.clear()
    assert [t["id"] for t in ksa.session_tasks(["s1"])] == ["t_remote"]
    assert calls == [False]  # every other reader keeps read-your-writes


def test_session_tasks_stale_ok_does_not_wait_behind_a_ledger_rebuild(live, kanban_home, monkeypatch, tmp_path):
    """A rebuild holding ``_links_lock`` (a board write moved the fingerprint) must not hold the strip."""
    import threading
    rep = tmp_path / "default.db"
    _replica(rep, [("t_remote", "from replica", "running", OTHER)], [("t_remote", ME, "s1", "shepherd", 0)])
    _switch_to_replica(kanban_home, monkeypatch, rep)
    monkeypatch.setattr(ksa, "_remote_links", _real_remote_links)  # the real cache + lock, not the stub
    monkeypatch.setattr(ksa, "_project_remote_links", lambda: [_ledger_row("t_remote", "s1")])
    fp = ["v1"]
    monkeypatch.setattr(ksa, "_links_fingerprint", lambda: (fp[0],))
    assert [t["id"] for t in ksa.session_tasks(["s1"], stale_ok=True)] == ["t_remote"]  # primes the projection
    fp[0] = "v2"  # a board write: the projection is stale and a rebuild is due
    out: list = []
    with ksa._links_lock:  # a rebuild in flight
        th = threading.Thread(target=lambda: out.append(ksa.session_tasks(["s1"], stale_ok=True)), daemon=True)
        th.start()
        th.join(timeout=3)
        finished = not th.is_alive()
    th.join(timeout=5)
    assert finished, "stale_ok session_tasks waited on _links_lock"
    assert [t["id"] for t in out[0]] == ["t_remote"]


_real_remote_links = ksa._remote_links
