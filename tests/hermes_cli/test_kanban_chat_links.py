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


@pytest.mark.parametrize('stale_ok', [True, False])
def test_unreadable_replica_fallback_preserves_stale_policy(tmp_path, monkeypatch, stale_ok):
    from hermes_cli import profiles

    calls = []

    def remote_links(stale_ok=False):
        calls.append(stale_ok)
        return [_ledger_row('t_prior', 's1')]

    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(ksa, '_remote_links', remote_links)
    links._reset_for_tests()  # no earlier projection: the profile-board read keeps the caller's policy too
    rows = links.replica_cards(['s1'], ME, replicas=[('default', tmp_path / 'absent.db')], stale_ok=stale_ok)
    assert calls and set(calls) == {stale_ok}  # the cold fallback and the profile-board read both keep the policy
    assert [(r['id'], r['stale'], r['source']) for r in rows] == [('t_prior', True, 'ledger-fallback')]


def test_unreadable_replica_and_cold_ledger_failure_is_not_empty(tmp_path, monkeypatch):
    def fail(stale_ok=False):
        raise sqlite3.OperationalError('ledger unreadable')

    monkeypatch.setattr(ksa, '_remote_links', fail)
    with pytest.raises(sqlite3.OperationalError, match='ledger unreadable'):
        links.replica_cards(['s1'], ME, replicas=[('default', tmp_path / 'absent.db')], stale_ok=True)


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
    refuse them (4132). No worker link is written; provenance belongs in task events, not chat ownership."""
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
    assert _links(live) == {(mine, "s_chat", "shepherd", 0)}


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


def test_reconcile_releases_the_owners_created_link_once_the_card_is_closed(live, kanban_home, monkeypatch):
    """Hub cards t_01a1842a037068ba et al. (2026-11-01): the hub wrote a ``created`` link for a macbook chat
    (kanban_creator_chats), the macbook ledger had no row for that chat+card, and no reconcile ever released it, so
    each done card stayed an ``unknown`` obligation in replica_cards. The chat's gateway now releases it under the
    same uid once the replica shows the card closed; open cards, ledger-held links and local cards are untouched."""
    rep = ksc.shadow_path("default")
    _replica(rep, [("t_done", "done", "done", OTHER), ("t_archived", "archived", "archived", OTHER),
                   ("t_open", "open", "running", OTHER), ("t_held", "held", "done", OTHER),
                   ("t_local", "local", "done", ME)],
             [("t_done", ME, "s1", "created", 0), ("t_archived", ME, "s1", "created", 0),
              ("t_open", ME, "s1", "created", 0), ("t_held", ME, "s1", "created", 0),
              ("t_local", ME, "s1", "created", 0), ("t_done", "mini", "s9", "created", 0),
              ("t_gone", ME, "s1", "created", 0)])  # t_gone: no card row in the replica, stays unknown
    held = {**_ledger_row("t_held", "s1", via="origin", status="unknown"), "unfinished": True}
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [held])
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    assert links.reconcile(live, "default", ME)["added"] == 3
    assert _links(live) == {("t_done", "s1", "created", 1), ("t_archived", "s1", "created", 1),
                            ("t_held", "s1", "created", 0)}
    uid = links.link_uid("t_done", ME, "default", "s1", "created")
    assert live.execute("SELECT released FROM task_chat_links WHERE uid = ?", (uid,)).fetchone()[0] == 1  # same row
    pushed = {r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name = 'task_chat_links'")}
    assert uid in pushed  # captured, so the Board's (owner-written) row turns released
    assert links.reconcile(live, "default", ME) == {"added": 0, "changed": 0, "released": 0, "retired": 0}
    # Before the push comes back: the replica still lists the done cards. After it (the same uids now released),
    # only the open card, the ledger-held one and the missing row remain this chat's.
    before = {c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])}
    assert {"t_done", "t_archived"} <= before
    conn = sqlite3.connect(rep)
    conn.execute("UPDATE task_chat_links SET released = 1 WHERE uid IN (?, ?)",
                 (uid, links.link_uid("t_archived", ME, "default", "s1", "created")))
    conn.commit()
    conn.close()
    after = {c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])}
    assert after == {"t_open", "t_held", "t_gone"}
    assert links.reconcile(live, "default", ME) == {"added": 0, "changed": 0, "released": 0, "retired": 0}


def test_replica_answers_a_chats_open_cards_on_other_gateways(tmp_path):
    rep = tmp_path / "default.db"
    _replica(rep, [("t_open", "open card", "running", OTHER), ("t_done", "done card", "done", OTHER),
                   ("t_mine", "local card", "running", ME), ("t_released", "released", "ready", OTHER),
                   ("t_theirs", "other gateway's chat", "ready", OTHER)],
             [("t_open", ME, "s1", "shepherd", 0), ("t_done", ME, "s1", "shepherd", 0), ("t_mine", ME, "s1", "created", 0),
              ("t_released", ME, "s1", "shepherd", 1), ("t_theirs", "mini", "s1", "shepherd", 0)])
    got = links.replica_cards(["s1", "s0"], ME, replicas=[("default", rep)])
    # A done card whose link is not released is still the chat's (done_pending_release, unfinished): the sidebar counts
    # it, so the list must too. Only a released link drops a card (t_released).
    assert sorted((c["id"], c["gateway"], c["board"], c["via"], c["session_id"], c["status"]) for c in got) == [
        ("t_done", OTHER, "default", "shepherd", "s1", "done_pending_release"),
        ("t_open", OTHER, "default", "shepherd", "s1", "running")]
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


def test_a_released_sibling_link_does_not_settle_a_closed_cards_unreleased_link(live, kanban_home, monkeypatch,
                                                                                 tmp_path):
    """[daveey] Each link carries its own answer: a released ``shepherd`` sibling no longer drops the same chat's
    unreleased ``created`` link to a closed card. The chat's gateway keeps a link unreleased only while its origin
    ledger holds the card unfinished (done, release unverified), and releases the owner's unmanaged ``created``
    link itself once the card closes (``_owner_written_releases``, the 2026-11-01 "4 open naming none" fix). The
    removed reader rule hid 19 done-but-unverified hub cards from David-MBP chat 20261026_170127_f07c32 that its
    ledger lists as ``unknown`` (links_diff not clean, 2026-11-13)."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_done", "done", "done", OTHER), ("t_arch", "archived", "archived", OTHER),
                   ("t_open", "open", "running", OTHER), ("t_settled", "settled", "done", OTHER)],
             [("t_done", ME, "s1", "created", 0), ("t_done", ME, "s1", "shepherd", 1),
              ("t_arch", ME, "s1", "created", 0), ("t_arch", ME, "s1", "shepherd", 1),
              ("t_open", ME, "s1", "created", 0), ("t_open", ME, "s1", "shepherd", 1),
              ("t_settled", ME, "s1", "created", 1), ("t_settled", ME, "s1", "shepherd", 1)])
    got = sorted((c["session"], c["id"], c["status"])
                 for c in links.replica_cards(None, ME, ksa.LISTED_STATUSES, replicas=[("default", rep)]))
    assert got == [("s1", "t_arch", "unknown"), ("s1", "t_done", "done_pending_release"), ("s1", "t_open", "running")]
    _switch_to_replica(kanban_home, monkeypatch, rep)
    assert ksa.active_task_counts() == {"s1": 3}
    assert sorted(c["id"] for c in ksa.session_tasks(["s1"])) == ["t_arch", "t_done", "t_open"]  # names its count


def test_tui_legacy_subs_on_done_local_cards_count_nothing(live, kanban_home, monkeypatch):
    """Legacy ``tui`` subs (no/invalid ``delivery_metadata``) keep an OPEN card counted, never a done one."""
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    done = kb.create_task(live, title="done", assignee="worker")
    still = kb.create_task(live, title="open", assignee="worker")
    with kbc.write_txn(live):
        for tid in (done, still):
            live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) "
                         "VALUES (?, 'tui', 's_legacy', 1)", (tid,))
            live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, delivery_metadata, created_at) "
                         "VALUES (?, 'tui', 's_junk', '{not json', 1)", (tid,))
        live.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (done,))
    ksa._reset_cache_for_tests()
    assert ksa.active_task_counts() == {"s_legacy": 1, "s_junk": 1}
    assert ksa.waiting_on_task_ids() == {"s_legacy": frozenset({still}), "s_junk": frozenset({still})}


def test_a_released_link_to_a_closed_card_stays_listed_as_done_for_a_reader_of_closed_cards(live, kanban_home,
                                                                                           monkeypatch, tmp_path):
    """[daveey] ``hermes kanban chat-tasks`` asks for done/archived cards too, yet a link released after its card
    closed made the card vanish from that list: worker-homed t_01a1d4e38cf61e24 (PR #2227) was ``[unknown]`` in the
    MacBook chat at 06:02 and gone by 06:06, and hub t_01a1d4f2f591d595, linked with ``chat-tasks --expect`` rc=0,
    dropped out (2026-11-13). The card stays as a ``done`` row with ``released`` and its ``release_reason``; the
    sidebar's open-status read, which never asks for closed cards, is unchanged."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_worker", "worker card", "done", "worker-2"), ("t_hub", "hub card", "done", OTHER),
                   ("t_arch", "archived", "archived", OTHER), ("t_open", "open", "running", OTHER),
                   ("t_elsewhere", "another chat's", "done", OTHER), ("t_mine", "local", "done", ME)],
             [("t_worker", ME, "s1", "created", 1), ("t_worker", ME, "s1", "shepherd", 1),
              ("t_hub", ME, "s1", "created", 0), ("t_hub", ME, "s1", "shepherd", 1),
              ("t_arch", ME, "s1", "shepherd", 1), ("t_open", ME, "s1", "shepherd", 0),
              ("t_elsewhere", ME, "s9", "created", 1), ("t_mine", ME, "s1", "created", 1)])
    closed = ksa.LISTED_STATUSES + ("done", "archived")
    got = {c["id"]: c for c in links.replica_cards(["s1"], ME, closed, replicas=[("default", rep)])}
    assert sorted((tid, c["status"], c["released"], c.get("release_reason")) for tid, c in got.items()) == [
        ("t_arch", "archived", True, "link_released"), ("t_hub", "done_pending_release", False, None),
        ("t_open", "running", False, None), ("t_worker", "done", True, "link_released")]
    assert got["t_worker"]["unfinished"] is False and got["t_worker"]["via"] == "origin"
    assert got["t_worker"]["session_id"] == "s1" and got["t_worker"]["source"] == "replica"
    # The open-status readers (sidebar counts, handover) still drop released links.
    assert sorted(c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])) == ["t_hub", "t_open"]
    _switch_to_replica(kanban_home, monkeypatch, rep)
    assert ksa.active_task_counts() == {"s1": 2}  # t_hub (done_pending_release) and t_open
    rows = {r["id"]: r["status"] for r in ksa.session_tasks(["s1"], closed)}
    assert rows == {"t_arch": "archived", "t_hub": "done_pending_release", "t_open": "running", "t_worker": "done"}


def _profile_board_row(task_id, session, profile="bb-tuner", **kw):
    """A qualified ledger row owned by a non-default profile: hermes-hub keeps that card in the profile's OWN kanban
    home (hub ``profiles/bb-tuner/kanban.db``), which Board sync never carries."""
    return {**_ledger_row(task_id, session, **kw), "profile": profile, "owner_profile": profile,
            "ownership_state": "qualified", "key": f"{OTHER}/{profile}/{kw.get('board', 'default')}/{task_id}"}


def test_a_profile_board_card_stays_the_chats_from_the_ledger_and_links_diff_is_clean(live, kanban_home, monkeypatch,
                                                                                     tmp_path):
    """[daveey] Hub card t_d3e3f88a lives in the hub's bb-tuner profile board, not the synced root board: the Board
    has no row for it and refused David-MBP's chat link (``not_found``, 2026-10-28). With ``links_reader: replica``
    the chat lost the card and links_diff stayed unclean on that one row (2026-11-13). The reader now keeps such a
    card from the ledger, and links_diff counts it as ``profile_board_links`` instead of a difference."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_open", "open", "running", OTHER)], [("t_open", ME, "s1", "shepherd", 0)])
    rows = [_ledger_row("t_open", "s1"), _profile_board_row("t_d3e3f88a", "s1", status="ready"),
            _profile_board_row("t_settled", "s1", status="done", released=True)]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    got = {c["id"]: c for c in links.replica_cards(["s1"], ME, ksa.LISTED_STATUSES, replicas=[("default", rep)])}
    assert sorted(got) == ["t_d3e3f88a", "t_open"]  # a released ledger row is no obligation
    assert got["t_d3e3f88a"]["source"] == links.PROFILE_BOARD_SOURCE
    assert (got["t_d3e3f88a"]["key"], got["t_d3e3f88a"]["status"]) == (f"{OTHER}/bb-tuner/default/t_d3e3f88a", "ready")
    assert got["t_open"]["source"] == "replica"
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    report = links.links_diff(ME, ksa.LISTED_STATUSES)
    assert report["clean"], report
    assert (report["profile_board_links"], report["ledger_links"], report["replica_links"]) == (1, 2, 2)
    (kanban_home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    links_reader: replica\n")
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    ksa._reset_cache_for_tests()
    assert sorted(t["id"] for t in ksa.session_tasks(["s1"])) == ["t_d3e3f88a", "t_open"]
    assert ksa.active_task_counts() == {"s1": 2}


def test_reconcile_never_writes_a_profile_board_cards_link_whether_or_not_the_ledger_lists_it(live, monkeypatch):
    """[daveey] The Board has no row for a profile-own board card, so it refuses every write of that card's link.
    The MacBook's link to hub ci-sweeper card t_225e0450 flipped unreleased/released as the ledger projection listed
    the card or not, and each flip was a refused op (8 in 3 h, 2026-11-24). Reconcile now leaves such a link as it
    is: neither ledger-driven writes nor releases, while an ordinary ledger link is still written and released."""
    flipping = links.link_uid("t_225e0450", ME, "default", "s_old", "created")
    steady = links.link_uid("t_d3e3f88a", ME, "default", "s1", "shepherd")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_225e0450', ?, 'default', 's_old', 'created', 1, 1)",
                     (flipping, ME))
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_d3e3f88a', ?, 'default', 's1', 'shepherd', 0, 1)",
                     (steady, ME))
        live.execute("DELETE FROM kanban_sync_outbox")
    listed = [_profile_board_row("t_225e0450", "s_old", profile="ci-sweeper", via="origin", status="archived"),
              _profile_board_row("t_d3e3f88a", "s1"), _ledger_row("t_open", "s1")]
    unlisted = listed[1:]
    rows = listed
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    for rows in (listed, unlisted, listed, unlisted):
        links.reconcile(live, "default", ME)
    queued = [r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name='task_chat_links'")]
    assert flipping not in queued and steady not in queued
    assert ("t_225e0450", "s_old", "created", 1) in _links(live) and ("t_d3e3f88a", "s1", "shepherd", 0) in _links(live)
    assert ("t_open", "s1", "shepherd", 0) in _links(live)  # an ordinary ledger link is still written
    rows = [_profile_board_row("t_d3e3f88a", "s1")]
    assert links.reconcile(live, "default", ME)["released"] == 1  # and t_open's is released once the ledger drops it
    assert ("t_open", "s1", "shepherd", 1) in _links(live) and ("t_d3e3f88a", "s1", "shepherd", 0) in _links(live)


def test_an_unreleased_profile_board_link_is_released_once_when_the_ledger_drops_it(live, monkeypatch):
    """A link left unreleased (written before this fix) whose card the ledger stops listing is released once, one
    refused write at most, and then stays put: no further pass queues it."""
    uid = links.link_uid("t_gone", ME, "default", "s1", "shepherd")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_gone', ?, 'default', 's1', 'shepherd', 0, 1)", (uid, ME))
        live.execute("DELETE FROM kanban_sync_outbox")
    rows = [_profile_board_row("t_gone", "s1")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    links.reconcile(live, "default", ME)
    assert ("t_gone", "s1", "shepherd", 0) in _links(live)  # listed: left alone
    rows = []
    for _ in range(3):
        links.reconcile(live, "default", ME)
    queued = [r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name='task_chat_links'")]
    assert queued == [uid] and ("t_gone", "s1", "shepherd", 1) in _links(live)


def test_a_profile_card_the_replica_carries_keeps_its_ledger_link(live, kanban_home, monkeypatch, tmp_path):
    """The writer uses the readers' test: a non-default profile's card the Board DOES carry (its replica has the task
    row) is no profile-own board card, so its ledger link is written as before."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_carried", "carried", "running", OTHER)], [])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [
        _profile_board_row("t_carried", "s1", profile="work"), _profile_board_row("t_d3e3f88a", "s1")])
    links.reconcile(live, "default", ME)
    assert _links(live) == {("t_carried", "s1", "shepherd", 0)}


def test_a_machine_session_link_to_a_profile_board_card_is_not_reported_as_a_release(live, monkeypatch):
    """Reconcile leaves a profile-board card's link alone, so the machine-session release receipt and the
    ``machine_links`` dry run do not list it either (it would be listed on every pass, never released)."""
    uid = links.link_uid("t_d3e3f88a", ME, "default", "s_machine", "shepherd")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_d3e3f88a', ?, 'default', 's_machine', 'shepherd', 0, 1)",
                     (uid, ME))
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_plain', ?, 'default', 's_machine', 'shepherd', 0, 1)",
                     (links.link_uid("t_plain", ME, "default", "s_machine", "shepherd"), ME))
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_profile_board_row("t_d3e3f88a", "s_machine")])
    monkeypatch.setattr("hermes_cli.kanban_db_notify.machine_sessions",
                        lambda gateway, refs, **kw: {str(r) for r in refs})
    assert [o["task_id"] for o in links.machine_links(live, "default", ME)] == ["t_plain"]


def _stale_row(task_id, session, profile="default"):
    """An origin-ledger row as ``kanban_origin_ledger.project`` gives it while this gateway's hermes-hub replica is
    more than 180 s old: every closed card it cannot re-verify reads ``unknown``, unfinished and stale."""
    return {**_ledger_row(task_id, session, via="origin", status="unknown"), "stale": True, "released": None,
            "profile": profile}


def test_a_stale_ledger_pass_never_reopens_released_links(live, monkeypatch):
    """[daveey] While the MacBook's hub replica was stale (hub outage, 2026-11-23) the origin ledger listed 531
    closed cards as unknown/unfinished; reconcile un-released their links, then released them again on the next
    fresh pass, ~1,035 Board writes per period, and the Board refused the writes for t_225e0450 (a ci-sweeper
    profile-board card whose stale row has no ``ownership_state``). A stale observation now changes no released
    link, whatever the card's board; a fresh unfinished row still reopens one."""
    carried = links.link_uid("t_closed", ME, "default", "s1", "created")
    sweeper = links.link_uid("t_225e0450", ME, "default", "s_old", "created")
    with kbc.write_txn(live):
        for uid, tid, sid in ((carried, "t_closed", "s1"), (sweeper, "t_225e0450", "s_old")):
            live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                         "released, created_at) VALUES (?, ?, ?, 'default', ?, 'created', 1, 1)", (uid, tid, ME, sid))
        live.execute("DELETE FROM kanban_sync_outbox")
    stale = [_stale_row("t_closed", "s1"), _stale_row("t_225e0450", "s_old", profile="ci-sweeper")]
    fresh: list = []  # a fresh projection drops a closed card (archived without a verified completion)
    rows = stale
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    for rows in (stale, fresh, stale, fresh, stale):
        links.reconcile(live, "default", ME)
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_chat_links'").fetchone()
    assert {("t_closed", "s1", "created", 1), ("t_225e0450", "s_old", "created", 1)} <= _links(live)
    rows = [{**_ledger_row("t_closed", "s1", via="origin", status="running"), "stale": False}]  # really reopened
    assert links.reconcile(live, "default", ME)["changed"] == 1
    assert ("t_closed", "s1", "created", 0) in _links(live)


def test_a_stale_legacy_row_still_reopens_its_link(live, monkeypatch):
    """The hold is for origin-ledger rows only: a legacy reader row's ``stale`` is the replica's sync age, and its
    terminal-carry reopen still un-releases the link, as before."""
    uid = links.link_uid("t_legacy", ME, "default", "s1", "shepherd")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_legacy', ?, 'default', 's1', 'shepherd', 1, 1)", (uid, ME))
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [
        {**_ledger_row("t_legacy", "s1", status="ready"), "stale": True}])
    assert links.reconcile(live, "default", ME)["changed"] == 1
    assert ("t_legacy", "s1", "shepherd", 0) in _links(live)


def test_only_a_link_that_only_stale_rows_want_is_held(live, monkeypatch):
    """Held per link: a fresh unfinished row for the same link still reopens it, and a fresh legacy ``shepherd``
    row of the same card does not un-hold the stale ``origin`` row's ``created`` link."""
    uid = links.link_uid("t_both", ME, "default", "s1", "created")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_both', ?, 'default', 's1', 'created', 1, 1)", (uid, ME))
    held = links.link_uid("t_mixed", ME, "default", "s3", "created")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, 't_mixed', ?, 'default', 's3', 'created', 1, 1)", (held, ME))
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [
        _stale_row("t_both", "s1"), {**_ledger_row("t_both", "s1", via="origin"), "stale": False},
        _stale_row("t_mixed", "s3"), {**_ledger_row("t_mixed", "s3", status="done", released=True), "stale": False}])
    links.reconcile(live, "default", ME)
    assert ("t_both", "s1", "created", 0) in _links(live)
    assert ("t_mixed", "s3", "created", 1) in _links(live) and ("t_mixed", "s3", "shepherd", 1) in _links(live)


def test_a_fresh_profile_board_row_without_ownership_state_writes_no_link(live, monkeypatch):
    """An origin-ledger row names its owner's exact profile but carries no ``ownership_state``: a non-default
    profile's card the replica lacks is a profile-own board card all the same, and its link is never written."""
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [
        {**_ledger_row("t_sweep", "s1", via="origin"), "profile": "ci-sweeper", "stale": False}])
    links.reconcile(live, "default", ME)
    assert not any(t == "t_sweep" for t, *_ in _links(live))


def test_links_diff_reports_stale_held_links_apart_from_differences(live, kanban_home, monkeypatch, tmp_path):
    """The writer keeps a released link released while only stale ledger rows want it, so ``links_diff`` lists that
    obligation as ``stale_held``, not ``only_in_ledger``. A fresh unfinished row the replica lacks is still a
    difference."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_closed", "closed", "done", OTHER)], [("t_closed", ME, "s1", "created", 1)])
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    rows = [_stale_row("t_closed", "s1")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    report = links.links_diff(ME, ksa.LISTED_STATUSES)
    assert report["clean"], report
    assert report["stale_held"] == {"s1": [(OTHER, "default", "t_closed")]} and report["stale_held_links"] == 1
    rows.append({**_ledger_row("t_lost", "s1", via="origin"), "stale": False})
    report = links.links_diff(ME, ksa.LISTED_STATUSES)
    assert not report["clean"] and report["only_in_ledger"] == {"s1": [(OTHER, "default", "t_lost")]}


def test_a_non_default_origin_row_the_replica_carries_keeps_its_link(live, monkeypatch, tmp_path):
    """The widened profile-board test still asks the replica: an origin-ledger card of a non-default profile that
    the Board carries is no profile-own board card, and its link is written."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_work", "work", "running", OTHER)], [])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [
        {**_ledger_row("t_work", "s1", via="origin"), "profile": "work", "stale": False}])
    links.reconcile(live, "default", ME)
    assert _links(live) == {("t_work", "s1", "created", 0)}


def test_the_writer_treats_another_gateways_non_default_origin_row_as_a_profile_card():
    """``_unwritable_profile_card``: another gateway's origin-ledger row of a non-default profile is a candidate
    even without ``ownership_state``; this gateway's own other-profile card and a default-profile card are not. The
    readers' ``_is_profile_board_card`` is unchanged: such a row stays an ordinary remote obligation there."""
    theirs = {**_ledger_row("t_x", "s1", via="origin"), "profile": "ci-sweeper"}
    assert links._unwritable_profile_card(theirs, ME) and not links._is_profile_board_card(theirs)
    assert not links._unwritable_profile_card({**theirs, "gateway": ME}, ME)
    assert not links._unwritable_profile_card({**theirs, "profile": "default"}, ME)
    assert links._unwritable_profile_card(_profile_board_row("t_q", "s1"), ME)


def test_links_diff_cli_prints_the_stale_held_links(monkeypatch, capsys):
    from hermes_cli import kanban_sync_cli
    report = {"clean": True, "chats": 1, "ledger_links": 1, "replica_links": 0, "machine_links": 0,
              "profile_board_links": 0, "profile_board": {}, "stale_held_links": 1,
              "stale_held": {"s1": [(OTHER, "default", "t_closed")]}, "only_in_ledger": {}, "only_in_replica": {}}
    monkeypatch.setattr(links, "links_diff", lambda *a, **k: report)
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    import argparse
    assert kanban_sync_cli._links_diff(argparse.Namespace(json=False, settle=0)) == 0
    out = capsys.readouterr().out
    assert "stale_held_links: 1" in out and "stale_held: 1 [('s1', ('hub', 'default', 't_closed'))]" in out


def test_a_replica_that_carries_a_profile_owned_card_keeps_its_own_answer(tmp_path, monkeypatch):
    """The ledger fallback is only for cards no readable replica carries: when the Board has the card (a profile
    the ledger names on a synced board), the replica's links decide, released ones included. A board no replica
    is read for (unsynced, or unreadable) still answers from the ledger."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_carried", "carried", "running", OTHER)], [("t_carried", ME, "s1", "shepherd", 1)])
    rows = [_profile_board_row("t_carried", "s1", profile="work"),
            _profile_board_row("t_elsewhere", "s1", board="runners")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    got = {c["id"]: c["source"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])}
    assert got == {"t_elsewhere": links.PROFILE_BOARD_SOURCE}
    assert links.replica_cards(["s0"], ME, replicas=[("default", rep)]) == []  # another chat's card


def test_replica_row_carries_the_owner_published_wait(tmp_path):
    """[daveey] A Board replica row has no park: the owner-published wait (``kanban_wait_replica.published_park``)
    reaches it from the origin-ledger card, only while the ledger reports the same status."""
    from hermes_cli.kanban_wait_info import describe_replica
    rep = tmp_path / "default.db"
    _replica(rep, [("t_sched", "parked", "scheduled", OTHER), ("t_moved", "moved on", "running", OTHER)],
             [("t_sched", ME, "s1", "shepherd", 0), ("t_moved", ME, "s1", "shepherd", 0)])
    wait = {"park": {"wake_at": 4102444800, "reason": "waits", "ended_at": 1, "outcome": None},
            "awaiting": {"kind": "card", "ref": "t_0123456789abcdef"},
            "wake_check": "hermes kanban wait-check task t_0123456789abcdef"}
    ledger = [{**_ledger_row("t_sched", "s1", status="scheduled"), "profile": "default",
               "reported_status": "scheduled", **wait},
              {**_ledger_row("t_moved", "s1", status="running"), "profile": "default",
               "reported_status": "scheduled", **wait}]
    got = {c["id"]: c for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)], origin_cards=ledger)}
    assert {k: got["t_sched"][k] for k in wait} == wait
    assert describe_replica(got["t_sched"])["waiting_on"] == "card t_0123456789abcdef"
    assert not {"park", "awaiting", "wake_check"} & set(got["t_moved"])  # published for an earlier status


def test_only_parked_ledger_cards_lend_their_wait(tmp_path):
    """A running card's run-derived ``park`` is not a wait: only scheduled and blocked kind=time ledger cards lend
    park/awaiting/wake_check to the replica row (``_ledger_index``)."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_run", "running", "running", OTHER), ("t_time", "time block", "blocked", OTHER),
                   ("t_ask", "needs input", "blocked", OTHER)],
             [(t, ME, "s1", "shepherd", 0) for t in ("t_run", "t_time", "t_ask")])
    wait = {"park": {"wake_at": 4102444800, "reason": "r", "ended_at": 1, "outcome": None},
            "awaiting": {"kind": "clock", "why": "cooldown"}}
    ledger = [{**_ledger_row(t, "s1", status=s), "profile": "default", "reported_status": s, "block_kind": k, **wait}
              for t, s, k in (("t_run", "running", None), ("t_time", "blocked", "time"),
                              ("t_ask", "blocked", "needs_input"))]
    got = {c["id"]: c for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)], origin_cards=ledger)}
    assert not {"park", "awaiting"} & set(got["t_run"]) and not {"park", "awaiting"} & set(got["t_ask"])
    assert {k: got["t_time"][k] for k in wait} == wait
