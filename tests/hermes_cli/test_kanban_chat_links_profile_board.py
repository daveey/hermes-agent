"""[daveey] The profile-board ledger fallback of ``replica_cards``/``links_diff`` (PR #2372), tightened by its review
5894115299: links_diff names the rows it trusts and does not trust a missing card of an owner whose cards reach the
Board, the profile-board read does not wait on the ledger projection for nothing, the carry probe keeps to the
lineage, and a closed-card reader sees a released profile-board card as it sees a released replica link."""

from __future__ import annotations

import argparse
import contextlib
import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_cli
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_chat_links import ME, OTHER, _ledger_row, _profile_board_row, _replica


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, "default", ME)
    ksa._reset_cache_for_tests()
    links._reset_for_tests()
    try:
        yield home
    finally:
        conn.close()
        ksa._reset_cache_for_tests()
        links._reset_for_tests()


def _diff_env(monkeypatch, kanban_home, rep, rows):
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))


def test_links_diff_names_trusted_profile_board_rows_and_flags_a_missing_card_of_a_synced_profile(
        kanban_home, monkeypatch, tmp_path, capsys):
    """Review 5894115299 (PR #2372): a ``work``-owned card on the shared board, missing from a readable replica, was
    counted as a profile-board link and the diff read clean, while the same row with the ``default`` owner read
    unclean. ``work`` has another card on that replica, so its cards reach the Board: the missing one is now a
    difference. A profile none of whose cards the replica carries (hub bb-tuner) stays trusted, and the report
    names it (``profile_board``), in the CLI text too."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_work_on", "work card", "running", OTHER)], [("t_work_on", ME, "s1", "shepherd", 0)])
    rows = [_profile_board_row("t_work_on", "s1", profile="work"),
            _profile_board_row("t_work_lost", "s1", profile="work"),
            _profile_board_row("t_d3e3f88a", "s1", status="ready")]
    _diff_env(monkeypatch, kanban_home, rep, rows)
    report = links.links_diff(ME, ksa.LISTED_STATUSES)
    assert not report["clean"], report
    assert report["only_in_ledger"] == {"s1": [(OTHER, "default", "t_work_lost")]}
    assert report["profile_board"] == {"s1": [(OTHER, "default", "t_d3e3f88a")]}
    assert report["profile_board_links"] == 1
    # The reader still lists the missing work card: the union never hides a card.
    got = {c["id"]: c for c in links.replica_cards(None, ME, ksa.LISTED_STATUSES, replicas=[("default", rep)])}
    assert sorted(got) == ["t_d3e3f88a", "t_work_lost", "t_work_on"]
    assert (got["t_work_lost"]["owner_on_board"], got["t_d3e3f88a"]["owner_on_board"]) == (True, False)
    capsys.readouterr()
    assert kanban_sync_cli._links_diff(argparse.Namespace(json=False)) == 1
    assert "profile_board: 1 [('s1', ('hub', 'default', 't_d3e3f88a'))]" in capsys.readouterr().out


def test_with_no_replica_rows_the_profile_board_read_does_not_wait_on_the_projection(kanban_home, monkeypatch,
                                                                                     tmp_path):
    """Review 5894115299 (PR #2372): ``replica_cards`` read the ledger on every call for its profile-board cards,
    so a call with no replica rows (and no ``stale_ok``) queued on a projection rebuild. While the last projection
    had no profile-board card, it now takes that projection (``stale_ok``); once it had one, it reads fresh."""
    rep = tmp_path / "default.db"  # no link of this gateway's chats at all
    _replica(rep, [("t_open", "open", "running", OTHER)], [("t_open", "elsewhere", "s9", "shepherd", 0)])
    rows = [_ledger_row("t_open", "s9")]
    calls: list[bool] = []

    def remote_links(stale_ok=False):
        calls.append(stale_ok)
        return rows

    monkeypatch.setattr(ksa, "_remote_links", remote_links)
    assert links.replica_cards(["s1"], ME, replicas=[("default", rep)]) == []
    assert calls == [False]  # no projection yet: read it
    calls.clear()
    assert links.replica_cards(["s1"], ME, replicas=[("default", rep)]) == []
    assert calls == [True]  # the last one had no profile-board card: never wait on a rebuild for it
    rows.append(_profile_board_row("t_pb", "s1"))
    links._reset_for_tests()
    links._ledger_index()  # a projection WITH a profile-board card
    calls.clear()
    assert [c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])] == ["t_pb"]
    assert calls == [False]


def test_the_carry_probe_keeps_to_the_lineage(kanban_home, monkeypatch, tmp_path):
    """Review 5894115299 (PR #2372): the carry probe ran for every chat's profile-board cards and the lineage
    filter came after it. A lineage read now probes only its own chats' cards."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_open", "open", "running", OTHER)], [("t_open", ME, "s1", "shepherd", 0)])
    rows = [_ledger_row("t_open", "s1"), _profile_board_row("t_mine", "s1"), _profile_board_row("t_theirs", "s2")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    probed: list[str] = []
    real = ksa.cached_ro

    @contextlib.contextmanager
    def traced(path, **kw):
        with real(path, **kw) as conn:
            conn.set_trace_callback(probed.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(ksa, "cached_ro", traced)
    got = sorted(c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)]))
    assert got == ["t_mine", "t_open"]
    carry = [s for s in probed if s.startswith("SELECT id FROM tasks WHERE id IN")]
    assert carry and all("'t_theirs'" not in s for s in carry) and any("'t_mine'" in s for s in carry), carry


def test_a_closed_card_reader_sees_a_released_profile_board_card_like_a_released_replica_link(
        kanban_home, monkeypatch, tmp_path):
    """Review 5894115299 (PR #2372): a reader that lists closed cards got a released replica link as an explicit
    released row (``_released_links``), but a released profile-board card not at all. Both are released rows now;
    the open-status readers (sidebar counts) still drop both."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_hub", "hub card", "done", OTHER)], [("t_hub", ME, "s1", "created", 1)])
    rows = [_ledger_row("t_hub", "s1", via="origin", released=True, status="done"),
            _profile_board_row("t_pb_done", "s1", status="done", released=True),
            _profile_board_row("t_pb_other", "s2", status="done", released=True)]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    closed = ksa.LISTED_STATUSES + ("done", "archived")
    got = {c["id"]: c for c in links.replica_cards(["s1"], ME, closed, replicas=[("default", rep)])}
    assert sorted(got) == ["t_hub", "t_pb_done"]
    for tid, source in (("t_hub", "replica"), ("t_pb_done", links.PROFILE_BOARD_SOURCE)):
        c = got[tid]
        assert (c["status"], c["released"], c["unfinished"], c["release_reason"], c["source"]) == (
            "done", True, False, "link_released", source), c
    assert links.replica_cards(["s1"], ME, replicas=[("default", rep)]) == []
    assert links.replica_cards(["s1"], ME, ("ready", "archived"), replicas=[("default", rep)]) == []


def test_an_unreadable_carry_probe_still_counts_as_carried(kanban_home, monkeypatch, tmp_path):
    """The carry probe's failure keeps its answer (carried, never a guess) after the lineage filter moved ahead."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_open", "open", "running", OTHER)], [("t_open", ME, "s1", "shepherd", 0)])
    rows = [_ledger_row("t_open", "s1"), _profile_board_row("t_pb", "s1")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)

    def broken(conn, ids, *, first=False):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(links, "_present", broken)
    assert sorted(c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])) == ["t_open"]
