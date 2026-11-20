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
        a, b, c = park(conn, 5001), park(conn, 5002), park(conn, 5003)
        gh.run_modes = {"5002": "5xx", "5003": "401"}  # b: one failed poll (unknown); c: auth failure (red)
        tick(conn, gh, clock, 0)
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
