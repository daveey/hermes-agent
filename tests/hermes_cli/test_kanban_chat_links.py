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
    assert links.reconcile(live, "default", ME) == {"added": 2, "changed": 0, "released": 0}
    assert _links(live) == {(mine, "s_creator", "created", 0), (mine, "s_watcher", "shepherd", 0)}
    queued = [r[0] for r in live.execute("SELECT table_name FROM kanban_sync_outbox")]
    assert queued.count("task_chat_links") == 2  # pushed like any synced row
    assert links.reconcile(live, "default", ME) == {"added": 0, "changed": 0, "released": 0}  # idempotent
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_notify_subs")
    assert links.reconcile(live, "default", ME)["released"] == 1  # an unsubscribe releases, never deletes
    assert (mine, "s_watcher", "shepherd", 1) in _links(live)


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
    assert [(c["id"], c["gateway"], c["board"], c["via"], c["session_id"]) for c in got] == [("t_open", OTHER, "default", "shepherd", "s1")]
    assert got[0]["title"] == "open card" and got[0]["source"] == "replica"


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
