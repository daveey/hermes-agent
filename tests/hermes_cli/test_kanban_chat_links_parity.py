"""Sidebar counts and the session task list agree under ``kanban.sync.links_reader: replica``. [daveey]

The MacBook's Upkeep chat showed "Waiting on 14 cards" (counts read the origin-ledger projection) while the task
strip listed 9 (``session_tasks`` read the replica, which dropped the 5 done-but-unverified cards). Each case here
asserts the counted number equals the listed number for one chat lineage, with both read the way the Desktop
reads them (``annotate_kanban_activity`` and ``session_tasks``).
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

ME, HUB, MINI = "macbook", "hub", "mini"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    (home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    links_reader: replica\n")
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, "default", ME)
    conn.close()
    replicas: dict[str, object] = {}
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", home / "kanban.db"),
                                                     *((s, home / f"{s}.links.db") for s in replicas if s != "default")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: tmp_path / "shadow" / f"{slug}.db")
    ledger: list[dict] = []
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: list(ledger))
    ksa._reset_cache_for_tests()
    getattr(links, "_reset_for_tests", lambda: None)()
    yield {"tmp": tmp_path, "home": home, "ledger": ledger, "replicas": replicas}
    ksa._reset_cache_for_tests()
    getattr(links, "_reset_for_tests", lambda: None)()


def replica(env, tasks, chat_links, *, board="default", pulled_at: object = "now"):
    """``tasks``: (id, status, created_on[, block_kind]); ``chat_links``: (task_id, session, kind, released)."""
    env["replicas"][board] = True
    path = env["tmp"] / "shadow" / f"{board}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
        "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, chat_session TEXT, "
        "kind TEXT, released INTEGER, created_at INTEGER);"
        "CREATE TABLE kanban_sync_state (board_id TEXT PRIMARY KEY, cursor INTEGER, gateway_id TEXT, hlc_last TEXT, "
        "mode TEXT, applying INTEGER, pending_run_uid TEXT, pulled_at REAL);")
    for t in tasks:
        tid, status, owner, block_kind = (*t, None)[:4]
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, 'worker', 1, NULL, 0, ?, NULL, ?)",
                     (tid, f"title {tid}", status, block_kind, owner))
    for tid, session, kind, released in chat_links:
        conn.execute("INSERT INTO task_chat_links VALUES (?, ?, ?, 'default', ?, ?, ?, 1)",
                     (links.link_uid(tid, ME, "default", session, kind), tid, ME, session, kind, released))
    stamp = time.time() if pulled_at == "now" else pulled_at
    conn.execute("INSERT INTO kanban_sync_state VALUES (?, 0, ?, '', 'shadow', 0, NULL, ?)", (board, ME, stamp))
    conn.commit()
    conn.close()
    ksa._reset_cache_for_tests()
    return path


def counted(*lineage):
    row = {"id": lineage[-1], "_lineage_ids": list(lineage)}
    ksa.annotate_kanban_activity([row])
    return int(row.get("kanban_active") or 0) + int(row.get("kanban_blocked") or 0), row


def listed(*lineage):
    return [t for t in ksa.session_tasks(list(lineage)) if t.get("remote")]


def assert_parity(*lineage, expect):
    n, row = counted(*lineage)
    rows = listed(*lineage)
    assert (n, len(rows)) == (expect, expect), (row, [(r["key"], r["status"]) for r in rows])
    return row, rows


def test_one_active_and_twelve_todo_count_and_list_the_same(env):
    tasks = [("t_run", "running", HUB)] + [(f"t_todo{i:02d}", "todo", HUB) for i in range(12)]
    replica(env, tasks, [(t[0], "s1", "shepherd", 0) for t in tasks])
    assert_parity("s1", expect=13)


def test_blocked_for_review_counts_and_lists(env):
    replica(env, [("t_rev", "review", HUB), ("t_blk", "blocked", HUB, "needs_input")],
            [("t_rev", "s1", "shepherd", 0), ("t_blk", "s1", "created", 0)])
    assert_parity("s1", expect=2)


def test_done_without_verified_release_stays_an_unknown_chip_and_counts(env):
    """The Upkeep 14 vs 9: a done card whose link is not released is still an obligation in both places."""
    replica(env, [("t_open", "running", HUB), ("t_done", "done", HUB), ("t_arch", "archived", HUB)],
            [("t_open", "s1", "created", 0), ("t_done", "s1", "created", 0), ("t_arch", "s1", "shepherd", 0)])
    _, rows = assert_parity("s1", expect=3)
    done = {r["id"]: r for r in rows}
    assert (done["t_done"]["status"], done["t_done"]["reported_status"], done["t_done"]["unfinished"]) == ("unknown", "done", True)
    assert done["t_arch"]["status"] == "unknown"


def test_a_link_whose_card_row_is_missing_is_unknown_not_dropped(env):
    replica(env, [("t_open", "running", HUB)], [("t_open", "s1", "created", 0), ("t_gone", "s1", "created", 0)])
    _, rows = assert_parity("s1", expect=2)
    assert {r["id"]: r["status"] for r in rows}["t_gone"] == "unknown"


def test_verified_release_disappears_from_both(env):
    replica(env, [("t_done", "done", HUB), ("t_open", "ready", HUB)],
            [("t_done", "s1", "created", 1), ("t_open", "s1", "created", 0)])
    _, rows = assert_parity("s1", expect=1)
    assert [r["id"] for r in rows] == ["t_open"]


def test_another_chat_still_turns_done(env):
    replica(env, [("t_done", "done", HUB), ("t_open", "running", HUB)],
            [("t_done", "s2", "created", 1), ("t_open", "s1", "created", 0)])
    row, _ = assert_parity("s2", expect=0)
    assert "kanban_active" not in row and "kanban_remote_stale" not in row
    assert_parity("s1", expect=1)


def test_old_replica_rows_are_stale_from_its_real_sync_age(env):
    pulled = time.time() - 3600
    replica(env, [("t_open", "running", HUB)], [("t_open", "s1", "created", 0)], pulled_at=pulled)
    row, rows = assert_parity("s1", expect=1)
    assert rows[0]["stale"] is True and rows[0]["synced_at"] == pytest.approx(pulled)
    assert row.get("kanban_remote_stale") == 1


def test_fresh_replica_rows_are_not_stale(env):
    replica(env, [("t_open", "running", HUB)], [("t_open", "s1", "created", 0)])
    row, rows = assert_parity("s1", expect=1)
    assert rows[0]["stale"] is False and "kanban_remote_stale" not in row


def test_a_replica_that_never_recorded_a_pull_is_stale(env):
    replica(env, [("t_open", "running", HUB)], [("t_open", "s1", "created", 0)], pulled_at=None)
    _, rows = assert_parity("s1", expect=1)
    assert rows[0]["stale"] is True


def test_unreadable_replica_keeps_its_last_rows_as_stale(env):
    path = replica(env, [("t_open", "running", HUB), ("t_done", "done", HUB)],
                   [("t_open", "s1", "created", 0), ("t_done", "s1", "created", 0)])
    assert_parity("s1", expect=2)
    path.unlink()
    path.write_bytes(b"this is not a sqlite database" * 100)
    ksa._reset_cache_for_tests()
    _, rows = assert_parity("s1", expect=2)
    assert all(r["stale"] for r in rows)


def test_unreadable_replica_on_cold_start_falls_back_to_the_ledger_as_stale(env):
    path = env["tmp"] / "shadow" / "default.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage" * 200)
    env["ledger"].append({"id": "t_led", "board": "default", "gateway": HUB, "key": f"{HUB}/default/default/t_led",
                          "profile": "default", "session": "s1", "via": "origin", "origin_gateway": ME,
                          "origin_profile": "default", "released": False, "status": "unknown",
                          "reported_status": "done", "unfinished": True, "title": "t_led"})
    _, rows = assert_parity("s1", expect=1)
    assert rows[0]["stale"] is True and rows[0]["id"] == "t_led"


def test_same_task_id_on_two_gateways_is_two_rows(env):
    # Two boards (one per owner gateway's partition) carrying the same id.
    replica(env, [("t_same", "running", HUB)], [("t_same", "s1", "created", 0)])
    replica(env, [("t_same", "ready", MINI)], [("t_same", "s1", "shepherd", 0)], board="runners")
    _, rows = assert_parity("s1", expect=2)
    assert sorted(r["key"] for r in rows) == [f"{HUB}/default/default/t_same", f"{MINI}/default/runners/t_same"]


def test_one_owner_profile_per_card_even_when_the_ledger_names_several(env):
    """A synced board is one file per gateway with one row per id, so the replica holds ONE card: the ledger's other
    profile names (released rows, other chats) label it, they never fan it out into extra rows."""
    replica(env, [("t_same", "running", HUB)], [("t_same", "s1", "created", 0)])
    base = {"id": "t_same", "board": "default", "gateway": HUB, "via": "origin", "origin_gateway": ME,
            "origin_profile": "default", "status": "running", "unfinished": True}
    env["ledger"].extend([
        {**base, "profile": "alpha", "key": f"{HUB}/alpha/default/t_same", "session": "s_other", "released": False},
        {**base, "profile": "beta", "key": f"{HUB}/beta/default/t_same", "session": "s1", "released": True},
        {**base, "profile": "default", "key": f"{HUB}/default/default/t_same", "session": "s1", "released": False}])
    _, rows = assert_parity("s1", expect=1)
    assert (rows[0]["key"], rows[0]["owner_profile"]) == (f"{HUB}/default/default/t_same", "default")


def test_same_task_id_linked_from_two_origin_profiles_stays_with_each_profile(env, monkeypatch):
    path = replica(env, [("t_same", "running", HUB)], [("t_same", "s1", "created", 0)])
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO task_chat_links VALUES ('u2', 't_same', ?, 'alpha', 's9', 'created', 0, 1)", (ME,))
    conn.commit()
    conn.close()
    assert_parity("s1", expect=1)
    assert_parity("s9", expect=0)  # another origin profile's chat: not this profile's card
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alpha")
    ksa._reset_cache_for_tests()
    assert_parity("s9", expect=1)
    assert_parity("s1", expect=0)


def test_unreadable_replica_rows_are_kept_per_origin_profile(env, monkeypatch):
    path = replica(env, [("t_a", "running", HUB)], [("t_a", "s1", "created", 0)])
    assert_parity("s1", expect=1)
    path.unlink()
    path.write_bytes(b"garbage" * 200)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "alpha")
    ksa._reset_cache_for_tests()
    assert_parity("s1", expect=0)  # default's remembered rows are not alpha's


def test_owner_profile_comes_from_the_ledger_not_a_hard_coded_default(env):
    replica(env, [("t_a", "running", HUB)], [("t_a", "s1", "created", 0)])
    env["ledger"].append({"id": "t_a", "board": "default", "gateway": HUB, "profile": "alpha",
                          "key": f"{HUB}/alpha/default/t_a", "session": "s1", "via": "origin", "origin_gateway": ME,
                          "origin_profile": "default", "released": False, "status": "running", "unfinished": True})
    _, rows = assert_parity("s1", expect=1)
    assert rows[0]["key"] == f"{HUB}/alpha/default/t_a" and rows[0]["profile"] == "alpha"


def test_another_origin_profiles_links_are_not_this_profiles_cards(env):
    path = replica(env, [("t_a", "running", HUB)], [("t_a", "s1", "created", 0)])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE task_chat_links SET chat_profile = 'alpha'")
    conn.commit()
    conn.close()
    assert_parity("s1", expect=0)


def test_compression_lineage_counts_and_lists_every_alias(env):
    replica(env, [("t_root", "running", HUB), ("t_tip", "blocked", HUB, "needs_input"), ("t_both", "ready", HUB)],
            [("t_root", "s_root", "created", 0), ("t_tip", "s_tip", "created", 0),
             ("t_both", "s_root", "shepherd", 0)])
    assert_parity("s_root", "s_tip", expect=3)


def test_one_card_linked_from_two_sessions_of_a_lineage_counts_once(env):
    replica(env, [("t_both", "running", HUB), ("t_blk", "blocked", HUB, "needs_input")],
            [("t_both", "s_root", "created", 0), ("t_both", "s_tip", "shepherd", 0),
             ("t_blk", "s_root", "created", 0), ("t_blk", "s_tip", "shepherd", 0)])
    row, _ = assert_parity("s_root", "s_tip", expect=2)
    assert row.get("kanban_active") == 2  # not 4: each shared card once (blocked without a human ask is active)
    assert_parity("s_tip", expect=2)  # each session alone still counts both


def test_a_missing_card_row_the_ledger_places_on_this_gateway_is_not_remote(env):
    replica(env, [], [("t_mine", "s1", "created", 0)])
    env["ledger"].append({"id": "t_mine", "board": "default", "gateway": ME, "profile": "alpha",
                          "key": f"{ME}/alpha/default/t_mine", "session": "s1", "via": "origin", "origin_gateway": ME,
                          "origin_profile": "default", "released": False, "status": "running", "unfinished": True})
    assert_parity("s1", expect=0)


def test_links_diff_is_not_clean_while_a_replica_is_unreadable(env):
    path = env["tmp"] / "shadow" / "default.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage" * 200)
    env["ledger"].append({"id": "t_led", "board": "default", "gateway": HUB, "profile": "default",
                          "key": f"{HUB}/default/default/t_led", "session": "s1", "via": "origin",
                          "origin_gateway": ME, "origin_profile": "default", "released": False,
                          "status": "running", "unfinished": True})
    report = links.links_diff(ME)
    assert not report["clean"] and report["unreadable"] == ["default"]
    assert report["only_in_ledger"] == {"s1": [(HUB, "default", "t_led")]}  # never compared with itself


def test_links_diff_compares_unknown_unfinished_rows(env):
    replica(env, [("t_done", "done", HUB)], [("t_done", "s1", "created", 0)])
    row = {"id": "t_done", "board": "default", "gateway": HUB, "profile": "default", "key": f"{HUB}/default/default/t_done",
           "session": "s1", "via": "origin", "origin_gateway": ME, "origin_profile": "default", "released": False,
           "status": "unknown", "reported_status": "done", "unfinished": True}
    env["ledger"].append(row)
    assert links.links_diff(ME)["clean"]
    # The replica released it while the ledger still holds it unfinished: counts and list would split.
    replica(env, [("t_done", "done", HUB)], [("t_done", "s1", "created", 1)])
    report = links.links_diff(ME)
    assert not report["clean"]
    assert report["only_in_ledger"] == {"s1": [(HUB, "default", "t_done")]}


def _unknown_row(tid, session, via="origin"):
    return {"id": tid, "board": "default", "gateway": HUB, "profile": "default", "key": f"{HUB}/default/default/{tid}",
            "session": session, "via": via, "origin_gateway": ME, "origin_profile": "default", "released": False,
            "status": "unknown", "reported_status": "done", "unfinished": True}


def test_links_diff_agrees_on_a_done_unverified_card_whose_owner_written_sibling_was_released(env):
    """[daveey] David-MBP chat 20261026_170127_f07c32, 2026-11-13: 19 done hub cards whose origin ledger holds the
    ``created`` link unreleased (release unverified) while their owner-written ``shepherd`` link was released when
    the card closed. The replica dropped them through a released sibling, the ledger listed them ``unknown``."""
    replica(env, [("t_done", "done", HUB)], [("t_done", "s1", "created", 0), ("t_done", "s1", "shepherd", 1)])
    env["ledger"].append(_unknown_row("t_done", "s1"))
    report = links.links_diff(ME)
    assert report["clean"], report
    assert [(c["id"], c["status"]) for c in links.replica_cards(["s1"], ME)] == [("t_done", "unknown")]


def test_links_diff_leaves_out_placement_placeholder_chats(env):
    """[daveey] A ``kanban-placement*`` placeholder is no chat: the link writer never links it, and
    ``machine_sessions`` does not classify it machine, so only the placeholder filter keeps its ledger row out of
    the comparison. It is counted, not compared; a human chat beside it still differs."""
    replica(env, [("t_ph", "done", HUB), ("t_human", "done", HUB)], [("t_human", "s_human", "shepherd", 1)])
    env["ledger"].extend([_unknown_row("t_ph", "kanban-placement-abc"), _unknown_row("t_human", "s_human", "shepherd")])
    report = links.links_diff(ME)
    assert report["machine_links"] == 1, report
    assert report["only_in_ledger"] == {"s_human": [(HUB, "default", "t_human")]}


def test_links_diff_leaves_out_chats_the_link_writer_never_links(env):
    """[daveey] 250 of the 270 only-in-ledger links on David-MBP (2026-11-13) were kanban worker transcripts:
    ``_ledger_links`` never writes a machine session's link and ``reconcile`` releases one, so the ledger's rows for
    them differed by construction and hid the one real difference. They are counted, not compared."""
    worker = "local-" + "a" * 64  # a worker's headless hash: no interactive source proves it a chat
    replica(env, [("t_cron", "archived", HUB), ("t_hash", "done", HUB), ("t_human", "done", HUB)],
            [("t_cron", "cron_job_1", "shepherd", 1), ("t_human", "s_human", "shepherd", 1)])
    env["ledger"].extend([_unknown_row("t_cron", "cron_job_1", "shepherd"), _unknown_row("t_hash", worker),
                          _unknown_row("t_human", "s_human", "shepherd")])
    report = links.links_diff(ME)
    assert report["machine_links"] == 2
    assert report["only_in_ledger"] == {"s_human": [(HUB, "default", "t_human")]}  # a chat still differs
    assert not report["clean"] and report["chats"] == 1


def test_links_diff_a_missing_replica_with_only_machine_ledger_cards_stays_clean(env):
    """[daveey] Machine chats are popped before ``ledger_boards``: a missing replica whose only ledger cards are
    machine chats has nothing the writer would compare, so it never keeps the diff unclean. A human chat's card
    on that board still does (review 5890421655)."""
    replica(env, [], [])
    env["replicas"]["runners"] = True  # listed by board_paths, its shadow never pulled
    env["ledger"].append({**_unknown_row("t_cron", "cron_job_1", "shepherd"), "board": "runners"})
    report = links.links_diff(ME)
    assert (report["clean"], report["unreadable"], report["machine_links"]) == (True, [], 1), report
    env["ledger"].append({**_unknown_row("t_human", "s_human"), "board": "runners"})
    report = links.links_diff(ME)
    assert not report["clean"] and report["unreadable"] == ["runners"]


_ASK = {"action": "Name the build host or say wait", "why_human": "new runner registrations are approval-gated",
        "interaction": "reply", "scope": "one runner registration", "evidence": "runners API lists no canary"}


def _ask_on(path, *task_ids):
    conn = sqlite3.connect(path)
    conn.executemany("UPDATE tasks SET human_request = ? WHERE id = ?",
                     [(json.dumps(_ASK), tid) for tid in task_ids])
    conn.commit()
    conn.close()
    ksa._reset_cache_for_tests()


def test_remote_human_ask_with_null_block_kind_counts_blocked_not_open(env):
    """[daveey] Shape of hub runners/t_54aa263b and t_699fdca7: ``blocked``, ``block_kind`` NULL, a valid
    ``human_request`` stored as JSON text in the replica. The badge read them as open work (10 open / 0 blocked
    instead of 8 / 2), so the chat painted waiting instead of red."""
    path = replica(env, [("t_ask1", "blocked", HUB), ("t_ask2", "blocked", HUB), ("t_ops", "blocked", HUB),
                         ("t_run", "running", HUB)],
                   [(t, "s1", "shepherd", 0) for t in ("t_ask1", "t_ask2", "t_ops", "t_run")])
    _ask_on(path, "t_ask1", "t_ask2")
    row, rows = assert_parity("s1", expect=4)
    assert (row.get("kanban_active"), row.get("kanban_blocked")) == (2, 2), row
    assert {r["id"]: r["human_request"] for r in rows}["t_ask1"] == _ASK  # listed with the decoded ask


def test_local_card_and_remote_replica_cards_in_one_lineage(env):
    """[daveey] The live Upkeep shape: one local tracker card plus hub cards, linked across a compression lineage.
    Counts cover both stores once each; the strip lists the same set, and a remote human ask counts blocked."""
    from hermes_cli import kanban_db as kb

    conn = kbc.connect(env["home"] / "kanban.db")
    local = kb.create_task(conn, title="local tracker", assignee="default", session_id="s_root")
    conn.close()
    tasks = [("t_ask", "blocked", HUB)] + [(f"t_hub{i:02d}", "todo" if i else "running", HUB) for i in range(12)]
    path = replica(env, tasks, [(t[0], "s_tip" if t[0] == "t_ask" else "s_root", "shepherd", 0) for t in tasks])
    _ask_on(path, "t_ask")
    n, row = counted("s_root", "s_tip")
    every = ksa.session_tasks(["s_root", "s_tip"])
    assert n == len(every) == 14, (row, [(t.get("key") or t["id"], t["status"]) for t in every])
    assert (row.get("kanban_active"), row.get("kanban_blocked")) == (13, 1), row
    assert [t["id"] for t in every if not t.get("remote")] == [local]
    assert len(listed("s_root", "s_tip")) == 13
