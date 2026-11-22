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

kanban_home, gh, clock = helpers.kanban_home, helpers.gh, helpers.clock
park, tick = helpers.park, helpers.tick


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
        with kea._lock:
            kea._memo.clear()  # session_external's own 5 s memo predates this change; not under test
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
