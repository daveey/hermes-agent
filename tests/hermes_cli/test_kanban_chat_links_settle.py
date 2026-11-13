"""links_diff's settle window: a difference counts only when every read within the window lists it; sync lag
(fresh cards on busy chats, gone within 20-60 s) goes to ``in_flight`` (David-MBP live proof t_01a1d5c12a26ef3f,
2026-11-13: ``clean`` was False in every sample)."""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_chat_links_settle as settle
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_chat_links as base

ME, OTHER = base.ME, base.OTHER


@pytest.fixture
def clock(monkeypatch):
    """A fake window: ``sleep`` advances the clock and runs the next step (what the stores did meanwhile)."""
    state = {"now": 0.0, "step": 0, "sleeps": []}

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds
        state["step"] += 1

    monkeypatch.setattr(settle, "_sleep", sleep)
    monkeypatch.setattr(settle, "_clock", lambda: state["now"])
    return state


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    rep = tmp_path / "default.db"
    base._replica(rep, [("t_a", "a", "running", OTHER), ("t_b", "b", "ready", OTHER)],
                  [("t_a", ME, "s1", "shepherd", 0), ("t_b", ME, "s1", "shepherd", 0)])
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    ksa._reset_cache_for_tests()
    yield
    ksa._reset_cache_for_tests()


def _ledger(monkeypatch, clock, steps):
    """The origin ledger's answer at each step (the last one holds once the steps run out)."""
    def remote_links(stale_ok=False):
        return [base._ledger_row(*row[:2], **(row[2] if len(row) > 2 else {}))
                for row in steps[min(clock["step"], len(steps) - 1)]]
    monkeypatch.setattr(ksa, "_remote_links", remote_links)


SETTLED = [("t_a", "s1"), ("t_b", "s1")]


def test_a_fresh_card_the_replica_gets_within_the_window_is_in_flight_not_a_difference(board, monkeypatch, clock):
    # Read 1: the ledger already lists t_new, the replica has not pulled it; read 2: the ledger no longer
    # differs (the 2026-11-13 samples: each new card cleared within 20-60 s).
    _ledger(monkeypatch, clock, [SETTLED + [("t_new", "s1")], SETTLED])
    raw = links.links_diff(ME)
    assert not raw["clean"] and "in_flight" not in raw and "settle" not in raw  # the raw snapshot is unchanged
    clock["step"] = 0
    report = links.links_diff(ME, settle_s=90, poll_s=10)
    assert report["clean"] is True
    assert report["only_in_ledger"] == {} and report["only_in_replica"] == {}
    assert report["in_flight"] == {"only_in_ledger": {"s1": [(OTHER, "default", "t_new")]}, "only_in_replica": {}}
    assert report["settle"]["samples"] == 2 and clock["sleeps"] == [10]  # stops once nothing persists


def test_a_persistent_divergence_stays_unclean_through_the_window(board, monkeypatch, clock):
    # t_c is a link the replicas never get (the old hub t_d3e3f88a shape); t_new is lag next to it.
    stuck = ("t_c", "s1", {"board": "runners"})
    _ledger(monkeypatch, clock, [SETTLED + [stuck, ("t_new", "s1")], SETTLED + [stuck]])
    report = links.links_diff(ME, settle_s=30, poll_s=15)
    assert report["clean"] is False
    assert report["only_in_ledger"] == {"s1": [(OTHER, "runners", "t_c")]}
    assert report["in_flight"]["only_in_ledger"] == {"s1": [(OTHER, "default", "t_new")]}
    assert report["settle"]["samples"] == 3 and clock["now"] == 30  # read through the whole window


def _report(entries=(), unreadable=(), key="only_in_ledger"):
    out = {"only_in_ledger": {}, "only_in_replica": {}, "unreadable": list(unreadable), "chats": 1}
    for session, card in entries:
        out[key].setdefault(session, []).append(card)
    return out


def _run(clock, reads, settle_s=30.0):
    return settle.settle(lambda: reads[min(clock["step"], len(reads) - 1)], settle_s, 10.0)


def test_a_clean_first_read_answers_at_once(clock):
    report = _run(clock, [_report()])
    assert report["clean"] is True and clock["sleeps"] == [] and report["settle"]["samples"] == 1


def test_an_entry_only_a_later_read_lists_is_in_flight(clock):
    old, new = ("s1", ("hub", "default", "t_old")), ("s2", ("hub", "default", "t_new"))
    report = _run(clock, [_report([old]), _report([new])])
    assert report["clean"] is True
    assert report["in_flight"]["only_in_ledger"] == {"s1": [old[1]], "s2": [new[1]]}


def test_an_entry_that_clears_and_comes_back_is_not_persistent(clock):
    card = ("s1", ("hub", "default", "t_x"))
    other = ("s9", ("hub", "default", "t_keep"))
    report = _run(clock, [_report([card, other]), _report([other]), _report([card, other])])
    assert report["only_in_ledger"] == {"s9": [other[1]]} and report["in_flight"]["only_in_ledger"] == {
        "s1": [card[1]]}


def test_only_in_replica_settles_the_same_way(clock):
    card = ("s1", ("hub", "default", "t_r"))
    report = _run(clock, [_report([card], key="only_in_replica"), _report()])
    assert report["clean"] is True and report["in_flight"]["only_in_replica"] == {"s1": [card[1]]}
    report = (clock.update(step=0), _run(clock, [_report([card], key="only_in_replica")]))[1]
    assert report["clean"] is False and report["only_in_replica"] == {"s1": [card[1]]}


def test_an_unreadable_replica_is_unclean_only_while_the_last_read_fails(clock):
    report = _run(clock, [_report(unreadable=["default"]), _report()])
    assert report["clean"] is True and report["unreadable"] == []
    clock.update(step=0, now=0.0)
    report = _run(clock, [_report(unreadable=["default"])])
    assert report["clean"] is False and report["unreadable"] == ["default"] and clock["now"] == 30


def test_a_read_that_cannot_read_the_board_neither_clears_nor_confirms_its_entries(clock):
    # PR #2520 review repro: read 1 lists a difference on board B, read 2 cannot read B. A plain intersection
    # dropped the entry and the unreadable board both, so the window ended clean: True.
    stuck = ("s1", ("hub", "B", "t_x"))
    report = _run(clock, [_report([stuck], key="only_in_replica"), _report(unreadable=["B"])])
    assert report["clean"] is False and report["unreadable"] == ["B"]
    assert report["only_in_replica"] == {"s1": [stuck[1]]}
    # B readable again and still listing it: a persistent difference, not in flight.
    clock.update(step=0, now=0.0)
    report = _run(clock, [_report([stuck], key="only_in_replica"), _report(unreadable=["B"]),
                          _report([stuck], key="only_in_replica")])
    assert report["clean"] is False and report["unreadable"] == []
    assert report["only_in_replica"] == {"s1": [stuck[1]]} and report["in_flight"]["only_in_replica"] == {}
    # Lag on B in read 1, B unreadable in read 2 (second repro): unclean while B cannot be read.
    clock.update(step=0, now=0.0)
    report = _run(clock, [_report([stuck]), _report(unreadable=["B"])])
    assert report["clean"] is False
    # An unreadable B does not hold up entries on a readable board: they still clear as lag.
    clock.update(step=0, now=0.0)
    lag = ("s2", ("hub", "default", "t_lag"))
    report = _run(clock, [_report([lag]), _report(unreadable=["B"])])
    assert report["only_in_ledger"] == {} and report["in_flight"]["only_in_ledger"] == {"s2": [lag[1]]}
    assert report["clean"] is False and report["unreadable"] == ["B"]


def test_a_difference_on_a_board_first_unreadable_counts_from_its_first_readable_read(clock):
    # Read 1 cannot read B, reads 2-3 list a difference on it: persistent, not lag (else it ended clean).
    stuck = ("s1", ("hub", "B", "t_x"))
    report = _run(clock, [_report(unreadable=["B"]), _report([stuck]), _report([stuck])])
    assert report["clean"] is False and report["only_in_ledger"] == {"s1": [stuck[1]]}
    # Listed only while B was unreadable, never once B could be read: not a difference.
    clock.update(step=0, now=0.0)
    report = _run(clock, [_report([stuck], unreadable=["B"]), _report()])
    assert report["clean"] is True and report["in_flight"]["only_in_ledger"] == {"s1": [stuck[1]]}


def test_the_last_sleep_stops_at_the_window(clock):
    _run(clock, [_report([("s1", ("hub", "default", "t_x"))])], settle_s=25.0)
    assert clock["sleeps"] == [10.0, 10.0, 5.0]


def test_poll_is_two_sync_intervals_at_least_five_seconds(monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, "sync_config", lambda: {"interval_seconds": 7})
    assert settle.default_poll_s() == 14
    monkeypatch.setattr(sync, "sync_config", lambda: {"interval_seconds": 1})
    assert settle.default_poll_s() == 5
    monkeypatch.setattr(sync, "sync_config", dict)
    assert settle.default_poll_s() == 2 * ksc.DEFAULT_INTERVAL_S
    monkeypatch.setattr(sync, "sync_config", lambda: {"interval_seconds": "soon"})
    assert settle.default_poll_s() == 2 * ksc.DEFAULT_INTERVAL_S


def test_cli_settles_by_default_and_settle_zero_is_one_read(board, monkeypatch, clock, capsys):
    from hermes_cli import kanban as kc
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    _ledger(monkeypatch, clock, [SETTLED + [("t_new", "s1")], SETTLED])

    def cli(*argv):
        parser = argparse.ArgumentParser()
        kc.build_parser(parser.add_subparsers(dest="cmd"))
        return kc.kanban_command(parser.parse_args(["kanban", *argv]))

    assert cli("sync", "links-diff", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["clean"] is True and out["settle"]["window_s"] == settle.DEFAULT_SETTLE_S
    assert out["in_flight"]["only_in_ledger"] == {"s1": [[OTHER, "default", "t_new"]]}
    clock["step"] = 0
    assert cli("sync", "links-diff", "--settle", "0") == 1  # one raw read: the lagging card is a difference
    assert "only_in_ledger: 1" in capsys.readouterr().out
    clock["step"] = 0
    assert cli("sync", "links-diff") == 0
    assert "in_flight ledger: 1" in capsys.readouterr().out
