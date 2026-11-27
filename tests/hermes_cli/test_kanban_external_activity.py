"""Session-row counts and cards for external-run cards (interface.md + interface-hover.md). [daveey]"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_activity as kea
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_external_watch as helpers
from tests.hermes_cli import test_profiles_sidebar_scope as sidebar

kanban_home, gh, clock = helpers.kanban_home, helpers.gh, helpers.clock
park, tick = helpers.park, helpers.tick
profiles_on_disk = sidebar.profiles_on_disk


@pytest.fixture(autouse=True)
def _fresh():
    kea.reset_for_tests()
    yield
    kea.reset_for_tests()


def _rows_for(conn, monkeypatch, pairs, rows):
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [Path(str(kbc._main_db_file(conn)))])
    monkeypatch.setattr(ksa, "_parked_pairs", lambda c: set(pairs))
    kea.stamp_boards(rows, ksa._board_db_paths, ksa._open_ro, ksa._parked_pairs, ksa._identity.row_ids)
    return rows


def test_counts_are_subsets_and_cards_carry_service_status_and_check_times(kanban_home, gh, clock, monkeypatch):
    with kbc.connect_closing() as conn:
        a, b = park(conn, 5001), park(conn, 5002)
        # another repo's credential is refused: the first of its two cards is woken as the blocker's probe, the
        # other stays parked and red behind it
        other = [park(conn, n, ref=f"https://github.com/acme/other/actions/runs/{n}") for n in (5003, 5004)]
        gh.run_modes = {"5002": "5xx", "5003": "401", "5004": "401"}  # b: one failed poll (unknown)
        tick(conn, gh, clock, 0)
        (c,) = [t for t in other if helpers.status(conn, t) == "scheduled"]
        rows = _rows_for(conn, monkeypatch, [("s1", a), ("s1", b), ("s1", c), ("s2", a)],
                         [{"id": "s1", "kanban_parked": 3}, {"id": "s3", "kanban_parked": 1}])
        row = rows[0]
        assert (row["kanban_external"], row["kanban_external_unknown"], row["kanban_external_down"]) == (2, 1, 1)
        assert "kanban_blocked" not in row
        cards = row["kanban_external_cards"]
        assert {card["task_id"] for card in cards} == {a, b, c}
        for card in cards:
            ext = card["external"]
            assert card["title"].startswith("card ")
            for key in ("service", "job", "health", "reason", "observed_at", "next_due_at"):
                assert ext[key] not in (None, ""), (card["task_id"], key)
            assert ext["service"] == "GitHub Actions"
        # a session with no external card gets no field at all (absent = today's behaviour)
        assert not any(k.startswith("kanban_external") for k in rows[1])


def test_cards_are_capped_at_ten_newest_first(kanban_home, gh, clock, monkeypatch):
    with kbc.connect_closing() as conn:
        tids = [park(conn, 6000 + i) for i in range(12)]
        tick(conn, gh, clock, 0)
        kcp.sweep(conn, fetch=gh.fetch, fetch_run=gh.fetch_run, fetch_runners=lambda repo: [])  # the other six
        rows = _rows_for(conn, monkeypatch, [("s1", t) for t in tids], [{"id": "s1", "kanban_parked": 12}])
        row = rows[0]
        assert row["kanban_external"] == 12
        cards = row["kanban_external_cards"]
        assert len(cards) == kea.CARDS_CAP == 10
        seen = [c["external"]["observed_at"] for c in cards]
        assert seen == sorted(seen, reverse=True)


def test_every_external_cards_wait_info_carries_service_status_and_check_times(kanban_home, gh, clock):
    from hermes_cli.kanban_session_wait_rows import _local_wait_info
    with kbc.connect_closing() as conn:
        tids = [park(conn, 7000 + i) for i in range(3)]
        tick(conn, gh, clock, 0)
        info = _local_wait_info(conn, tids, now=clock.now + 1)
        assert set(info) == set(tids)
        for tid in tids:
            ext = info[tid]["external"]
            assert info[tid]["wait_state"] == "external_running"
            assert ext["service"] and ext["health"] and ext["job"]
            assert ext["observed_at"] and ext["next_due_at"] and ext["next_due_at"] > ext["observed_at"]


def test_a_card_before_its_first_poll_has_service_and_status_but_null_check_times(kanban_home, clock):
    from hermes_cli.kanban_session_wait_rows import _local_wait_info
    with kbc.connect_closing() as conn:
        tid = park(conn)
        ext = _local_wait_info(conn, [tid], now=time.time() + 5)[tid]["external"]
        assert ext["service"] == "GitHub Actions" and ext["health"] == "ok" and ext["reason"]
        assert ext["observed_at"] is None and ext["next_due_at"] is None


def test_non_external_waits_keep_todays_wait_info(kanban_home):
    from hermes_cli.kanban_session_wait_rows import _local_wait_info
    with kbc.connect_closing() as conn:
        tid = park(conn, kind="clock", ref="soak", wake_check=None, clock_only=True)
        info = _local_wait_info(conn, [tid])[tid]
        assert "external" not in info and info["wait_state"] in ("waiting", "stalled")


# --- the session list opens a board only when it moved (t_01a204267fa8a30d) ---------------------------------------

def _counting(monkeypatch, conn):
    """Point the stamp at ``conn``'s board with the real readers; returns (stamp, opens)."""
    path = Path(str(kbc._main_db_file(conn)))
    opens: list[Path] = []
    real_open = ksa._open_ro

    def open_ro(p):
        opens.append(Path(p))
        return real_open(p)

    def stamp(rows):
        return kea.stamp_boards(rows, lambda: [path], open_ro, ksa._parked_pairs, ksa._identity.row_ids)
    return stamp, opens


def _fresh_stamp(conn, rows):
    """What the pre-cache code answered: every board opened and read."""
    kea.reset_for_tests()
    path = Path(str(kbc._main_db_file(conn)))
    return kea.stamp_boards(rows, lambda: [path], ksa._open_ro, ksa._parked_pairs, ksa._identity.row_ids,
                            signature=None)


def _session_card(conn, sid, **kw):
    tid = kb.create_task(conn, title=f"card of {sid}", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', session_id=? WHERE id=?", (sid, tid))
    return tid


def test_an_unchanged_board_is_not_opened_again_and_a_write_to_it_is_seen_at_once(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        _session_card(conn, "s1")  # parked, but nothing scheduled: no external state, no clock input
        stamp, opens = _counting(monkeypatch, conn)
        first = stamp([{"id": "s1"}])
        assert len(opens) == 1
        for _ in range(20):
            assert stamp([{"id": "s1"}]) == first
        assert len(opens) == 1, "an unchanged board must not be opened per request"
        assert first == _fresh_stamp(conn, [{"id": "s1"}])
        kea.reset_for_tests()
        stamp([{"id": "s1"}])
        before = len(opens)
        _session_card(conn, "s1")  # any commit to the board moves its signature
        stamp([{"id": "s1"}])
        assert len(opens) == before + 1
        stamp([{"id": "s1"}])
        assert len(opens) == before + 1


def test_a_cached_board_answers_byte_identically_and_a_new_external_card_shows_on_the_next_request(
        kanban_home, gh, clock, monkeypatch):
    import json
    with kbc.connect_closing() as conn:
        a = park(conn, 8001)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id='s1' WHERE id=?", (a,))
        tick(conn, gh, clock, 0)
        stamp, opens = _counting(monkeypatch, conn)
        rows = lambda: [{"id": "s1", "kanban_parked": 1}, {"id": "s2"}]
        cold = json.dumps(stamp(rows()), sort_keys=True)
        warm = json.dumps(stamp(rows()), sort_keys=True)
        assert len(opens) == 1 and warm == cold
        assert cold == json.dumps(_fresh_stamp(conn, rows()), sort_keys=True)
        assert json.loads(cold)[0]["kanban_external"] == 1
        # a second external card lands: the very next request shows it (no TTL wait)
        kea.reset_for_tests()
        stamp(rows())
        b = park(conn, 8002)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id='s1' WHERE id=?", (b,))
        tick(conn, gh, clock, 0)
        got = stamp(rows())
        assert got[0]["kanban_external"] == 2
        assert json.dumps(got, sort_keys=True) == json.dumps(_fresh_stamp(conn, rows()), sort_keys=True)


def test_a_board_with_a_scheduled_card_is_reread_after_the_ttl_because_its_state_moves_with_the_clock(
        kanban_home, gh, clock, monkeypatch):
    with kbc.connect_closing() as conn:
        a = park(conn, 8101)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id='s1' WHERE id=?", (a,))
        tick(conn, gh, clock, 0)
        stamp, opens = _counting(monkeypatch, conn)
        wall = [time.time()]
        monkeypatch.setattr(kea.time, "time", lambda: wall[0])
        stamp([{"id": "s1"}])
        stamp([{"id": "s1"}])
        assert len(opens) == 1
        wall[0] += kea._TTL_S + 0.1
        stamp([{"id": "s1"}])
        assert len(opens) == 2


def test_an_external_card_that_left_is_gone_on_the_next_request_and_stays_gone(kanban_home, gh, clock, monkeypatch):
    """The read a moved signature asks for must not be answered by ``session_external``'s own 5 s memo: that
    pre-commit answer would be kept under the new signature until the board's next commit."""
    with kbc.connect_closing() as conn:
        a = park(conn, 9001)
        other = kb.create_task(conn, title="blocked sibling", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id='s1' WHERE id=?", (a,))
            conn.execute("UPDATE tasks SET status='blocked', session_id='s1' WHERE id=?", (other,))
        tick(conn, gh, clock, 0)
        stamp, _opens = _counting(monkeypatch, conn)
        wall = [time.time()]
        monkeypatch.setattr(kea.time, "time", lambda: wall[0])
        rows = lambda: [{"id": "s1", "kanban_parked": 2}]
        assert stamp(rows())[0].get("kanban_external") == 1
        wall[0] += 2.0
        with kb.write_txn(conn):  # the last scheduled card leaves; a parked sibling keeps the board in play
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
        for step in (1.0, 6.0, 60.0, 600.0):
            wall[0] += step
            assert "kanban_external" not in stamp(rows())[0], step


def test_an_unknown_signature_reads_the_board_every_time(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        _session_card(conn, "s1")
        path = Path(str(kbc._main_db_file(conn)))
        opens = []

        def open_ro(p):
            opens.append(p)
            return ksa._open_ro(p)
        for _ in range(3):
            kea.stamp_boards([{"id": "s1"}], lambda: [path], open_ro, ksa._parked_pairs, ksa._identity.row_ids,
                             signature=lambda p: None)
        assert len(opens) == 3 and not kea._boards


# --- one response reads each board once (t_01a21ab4eb964866) --------------------------------------------------------

def test_every_stamp_in_one_block_shares_one_board_read_even_across_a_commit(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        _session_card(conn, "s1")
        stamp, opens = _counting(monkeypatch, conn)
        with kea.one_board_read():
            first = stamp([{"id": "s1"}])
            _session_card(conn, "s1")  # a board commit mid-response (the hub commits every 1-2 s)
            for _ in range(12):  # the projects tree's per-profile annotations, the sidebar's slices
                assert stamp([{"id": "s1"}]) == first
        assert len(opens) == 1, "one response must read a board once, however many annotations it stamps"


def test_a_commit_during_a_block_is_seen_by_the_next_response(kanban_home, gh, clock, monkeypatch):
    with kbc.connect_closing() as conn:
        a = park(conn, 8001)
        tick(conn, gh, clock, 0)
        pairs = {("s1", a)}
        monkeypatch.setattr(ksa, "_parked_pairs", lambda c: set(pairs))
        stamp, _opens = _counting(monkeypatch, conn)
        with kea.one_board_read():
            assert stamp([{"id": "s1"}])[0]["kanban_external"] == 1
            b = park(conn, 8002)
            tick(conn, gh, clock, 1)
            pairs.add(("s1", b))
        row = stamp([{"id": "s1"}])[0]  # notification loss would leave this at 1 until some later commit
        assert row["kanban_external"] == 2
        assert row == _fresh_stamp(conn, [{"id": "s1"}])[0]


def test_a_failed_read_in_a_block_is_not_shared(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        _session_card(conn, "s1")
        path = Path(str(kbc._main_db_file(conn)))
        calls = []

        def flaky(p):
            calls.append(p)
            if len(calls) == 1:
                raise ksa.sqlite3.OperationalError("database is locked")
            return ksa._open_ro(p)

        with kea.one_board_read():
            kea.stamp_boards([{"id": "s1"}], lambda: [path], flaky, ksa._parked_pairs, ksa._identity.row_ids)
            got = kea.stamp_boards([{"id": "s1"}], lambda: [path], flaky, ksa._parked_pairs, ksa._identity.row_ids)
        assert len(calls) == 2, "the next stamp in the block retries a failed read"
        assert got == _fresh_stamp(conn, [{"id": "s1"}])


def test_blocks_nest_and_another_thread_reads_on_its_own(kanban_home, monkeypatch):
    import threading

    with kbc.connect_closing() as conn:
        _session_card(conn, "s1")
        stamp, opens = _counting(monkeypatch, conn)
        with kea.one_board_read():
            stamp([{"id": "s1"}])
            with kea.one_board_read():
                stamp([{"id": "s1"}])
            stamp([{"id": "s1"}])  # the inner block's end does not drop the outer one's read
            assert len(opens) == 1
            _session_card(conn, "s1")
            worker = threading.Thread(target=lambda: stamp([{"id": "s1"}]))
            worker.start()
            worker.join()
            assert len(opens) == 2, "a block is this thread's response only"


def test_annotation_snapshot_opens_one_board_read():
    with ksa.one_annotation_snapshot():
        assert kea._block.reads == {}
        with ksa.one_annotation_snapshot():
            assert kea._block.reads == {}
    assert getattr(kea._block, "reads", None) is None


def test_projects_tree_reads_each_board_once_for_all_profiles(profiles_on_disk, monkeypatch, tmp_path):
    from hermes_cli.web_routers import profiles

    for name, home in profiles_on_disk.items():
        sidebar._seed_session(home, name + "-chat", source="cli", cwd="/fixture/repo")
    board = tmp_path / "board" / "kanban.db"
    reads = []

    def once(path, *_a):
        reads.append(path)
        return {}

    def annotate(rows, **_kw):  # the real annotation's board stamp, without the counts machinery
        return kea.stamp_boards(rows, lambda: [board], ksa._open_ro, ksa._parked_pairs, ksa._identity.row_ids)

    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(ksa, "annotate_kanban_activity", annotate)
    monkeypatch.setattr(kea, "_read_board_external_once", once)
    profiles.get_profiles_projects_tree()
    assert len(profiles_on_disk) > 1
    assert reads == [board], "every profile's annotation in one tree response shares one read of the board"
