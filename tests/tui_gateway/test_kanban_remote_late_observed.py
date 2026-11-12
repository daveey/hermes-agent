"""A remote notice this host observed while fresh is never settled silently. [daveey]

Hub card t_01a1c86d86cc1db7 (MacBook ledger, 2026-11-12): its done notice was first seen at 04:23:33 PST, refused
while its chat stayed busy, and at 10:23:35 (6 h 0 m 2 s later) ``collect`` settled it ``stale`` with no transcript
row and no log line. Past ``FRESH_S`` an OBSERVED state is now delivered once as a late notice; only a state seeded
by the first replica snapshot settles silently, and an observed one dropped past ``LATE_MAX_S`` logs one WARNING.
"""
import contextlib
import json
import logging
import sqlite3

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_kanban_remote_notices import CHAT, _card, _hub as _base_hub, _ledger_row as _maybe_row
from tui_gateway import kanban_remote_notices as krn

T0 = 1_800_000_000.0
TID = "t_01a1c86d86cc1db7"
REF = f"hub/default/default/{TID}"


def _ledger_row(ledger, session, ref) -> dict:
    row = _maybe_row(ledger, session, ref)
    assert row is not None, f"no ledger row for {session} {ref}"
    return row


def _hub(tmp_path, cards):
    path = _base_hub(tmp_path, cards)
    with sqlite3.connect(path) as conn:  # caught up: observe() records a baseline
        conn.executemany("INSERT INTO meta VALUES (?,?)", [("cursor", "100"), ("head", "100")])
    return path


def _arrive(replica, card):
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)", (card["key"], json.dumps(card["task"])))


@contextlib.contextmanager
def _chat(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, db.get_compression_tip(sid) or sid) if sid == CHAT else (None, None)

        yield db, opener


def _deliver(db, ledger, items):
    for item in items:
        keys = item.get("event_keys") or [item["event_key"]]
        db.append_kanban_notice(CHAT, item["text"], {"notice_id": keys[0], "kanban_event_keys": keys})
        assert krn.ack(item, ledger=ledger)


def _observed_then_starved(tmp_path, ledger):
    """Baseline at T0, the card turns done at T0+60 and is observed at T0+120, then every pass is refused busy
    (claim, release) until the notice is past FRESH_S."""
    replica = _hub(tmp_path, [])
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0) == []
    _arrive(replica, _card(TID, CHAT, status="done", at=T0 + 60, summary="shipped"))
    for at in (T0 + 120, T0 + 3600, T0 + 3 * 3600, T0 + 120 + krn.FRESH_S - 60):
        got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=at)
        assert len(got) == 1
        assert krn.release(got[0], ledger=ledger)  # the chat is busy: the turn refused it
    return replica


def _stale_rows(ledger):
    with sqlite3.connect(ledger) as conn:
        return conn.execute("SELECT COUNT(*) FROM notices WHERE delivered=2 AND IFNULL(settled,'stale')='stale'"
                            ).fetchone()[0]


def test_starved_observed_notice_is_delivered_once_late_by_collect(tmp_path):
    ledger = tmp_path / "ledger.db"
    with _chat(tmp_path) as (db, opener):
        replica = _observed_then_starved(tmp_path, ledger)
        late = T0 + 120 + krn.FRESH_S + 300  # past FRESH_S since the first observation; the chat is idle now
        got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=late)
        assert len(got) == 1, "an observed, undelivered notice must not be settled stale past FRESH_S"
        assert got[0]["text"].startswith("Late notices — remote updates:") and TID in got[0]["text"]
        _deliver(db, ledger, got)
        krn._reset_for_tests()  # a restarted backend: neither door offers it again
        assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=late + 60) == []
        assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                        ledger=ledger, now=late + 60) == []
        assert len(db.get_messages(CHAT)) == 1
    row = _ledger_row(ledger, CHAT, REF)
    assert row["delivered"] == krn.DELIVERED and row["settled"] is None
    assert _stale_rows(ledger) == 0


def test_starved_observed_notice_is_delivered_once_late_by_the_sweep(tmp_path):
    ledger = tmp_path / "ledger.db"
    with _chat(tmp_path) as (db, opener):
        replica = _observed_then_starved(tmp_path, ledger)
        late = T0 + 120 + krn.FRESH_S + 300  # the chat closed while busy: only the idle sweep can reach it
        out = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                       ledger=ledger, now=late)
        assert [item["wake"] for item in out] == ["persisted"]
        krn._reset_for_tests()
        assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                        ledger=ledger, now=late + 60) == []
        assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=late + 60) == []
        messages = db.get_messages(CHAT)
        assert len(messages) == 1 and "Late notices — remote updates:" in messages[0]["content"]
    assert _ledger_row(ledger, CHAT, REF)["delivered"] == krn.DELIVERED
    assert _stale_rows(ledger) == 0


def test_observed_notice_dropped_past_the_late_window_logs_one_warning(tmp_path, caplog):
    ledger = tmp_path / "ledger.db"
    with _chat(tmp_path) as (db, _opener):
        replica = _observed_then_starved(tmp_path, ledger)
        gone = T0 + 120 + 72 * 3600 + 600  # still refused until past the 72 h late window too
        with caplog.at_level(logging.INFO, logger=krn.logger.name):
            assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=gone) == []
            krn._reset_for_tests()
            assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=gone + 60) == []
        assert db.get_messages(CHAT) == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == krn.logger.name]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    text = warnings[0].getMessage()
    assert TID in text and CHAT in text
    assert "first seen 2027-01-15T08:02:00Z" in text  # T0 + 120, UTC
    assert "age 72h10m" in text
    row = _ledger_row(ledger, CHAT, REF)
    assert row["delivered"] == krn.SETTLED and row["settled"] == "expired"


@pytest.mark.parametrize("door", ["collect", "sweep"])
def test_seeded_state_older_than_fresh_still_settles_silently(tmp_path, caplog, door):
    """Guard: a state the first snapshot seeded (older than FRESH_S) is history, not a late notice."""
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [_card(TID, CHAT, status="done", at=T0 - 8 * 3600)])
    krn._reset_for_tests()
    with _chat(tmp_path) as (db, opener), caplog.at_level(logging.INFO, logger=krn.logger.name):
        for at in (T0, T0 + 60, T0 + krn.FRESH_S + 60):
            if door == "collect":
                assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=at) == []
            else:
                assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                                ledger=ledger, now=at) == []
        assert db.get_messages(CHAT) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == krn.logger.name]
    if door == "collect":
        assert _ledger_row(ledger, CHAT, REF)["delivered"] == krn.SETTLED


@pytest.mark.parametrize("door", ["collect", "sweep"])
def test_fresh_notice_delivered_normally_yields_exactly_one_row(tmp_path, door):
    """Guard: an observed notice delivered inside FRESH_S is one row, never repeated as a late one."""
    ledger = tmp_path / "ledger.db"
    replica = _hub(tmp_path, [])
    krn._reset_for_tests()
    with _chat(tmp_path) as (db, opener):
        assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0) == []
        _arrive(replica, _card(TID, CHAT, status="done", at=T0 + 60, summary="shipped"))
        if door == "collect":
            got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=T0 + 120)
            assert len(got) == 1 and not got[0]["text"].startswith("Late notices")
            _deliver(db, ledger, got)
        else:
            assert len(krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                                ledger=ledger, now=T0 + 120)) == 1
        for at in (T0 + 180, T0 + krn.FRESH_S + 600, T0 + 72 * 3600 + 600):  # literal: the guard runs on main too
            krn._reset_for_tests()
            assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=at) == []
            assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                            ledger=ledger, now=at) == []
        assert len(db.get_messages(CHAT)) == 1
    assert _ledger_row(ledger, CHAT, REF)["delivered"] == krn.DELIVERED
