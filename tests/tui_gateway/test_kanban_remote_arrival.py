"""Offline catch-up is new evidence, but a fresh host's retained history is not."""
import contextlib
import json
import sqlite3

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_kanban_remote_notices import CHAT, OTHER, _card, _hub as _base_hub
from tui_gateway import kanban_remote_notices as krn


def _hub(tmp_path, cards):
    path = _base_hub(tmp_path, cards)
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO meta VALUES (?,?)", [("cursor", "100"), ("head", "100")])
    return path


@pytest.mark.parametrize("door", ["collect", "sweep"])
@pytest.mark.parametrize("status", ["done", "blocked"])
def test_offline_arrival_once_after_baseline(tmp_path, door, status):
    terminal = 1_800_000_000.0
    clock = [terminal - 60]
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield db, sid

        def poll():
            if door == "collect":
                got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=clock[0])
                for claim in got:
                    key = claim["event_key"]
                    db.append_kanban_notice(CHAT, claim["text"], {"notice_id": key, "kanban_event_keys": [key]})
                    assert krn.ack(claim, ledger=ledger)
                return got
            return krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                            ledger=ledger, now=clock[0])

        assert poll() == []  # host was already observing before going offline
        clock[0] = terminal + 8 * 3600
        card = _card("t_arrived", CHAT, status=status, at=terminal)
        with sqlite3.connect(replica) as conn:
            conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)",
                         (card["key"], json.dumps(card["task"])))
        krn._reset_for_tests()  # a new backend after the offline interval
        assert len(poll()) == 1
        krn._reset_for_tests()
        assert poll() == []
        other_door = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=clock[0])
        assert other_door == []
        assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                        ledger=ledger, now=clock[0]) == []
        assert len(db.get_messages(CHAT)) == 1


@pytest.mark.parametrize("door", ["collect", "sweep"])
def test_bootstrap_history_stays_silent_across_chats_and_restart(tmp_path, door):
    now = 1_800_000_000.0
    cards = [_card("t_history", OTHER, status="done", at=now - 8 * 3600)]
    replica = _hub(tmp_path, cards)
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(OTHER, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield db, sid

        if door == "collect":
            assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
        else:
            assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                            ledger=ledger, now=now, live_ids={OTHER}) == []
        krn._reset_for_tests()
        assert krn.collect(OTHER, "default", hub_db=replica, ledger=ledger, now=now + 60) == []
        assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                        ledger=ledger, now=now + 60) == []
        assert db.get_messages(OTHER) == []


def test_release_and_replica_rewrite_do_not_extend_arrival_window(tmp_path):
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    card = _card("t_retry", CHAT, status="done", at=now - 8 * 3600)
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)", (card["key"], json.dumps(card["task"])))
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    assert len(got) == 1
    assert krn.release(got[0], ledger=ledger)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET seq=2")
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + krn.FRESH_S + 1) == []


def test_late_legacy_and_superseded_rows_do_not_get_local_freshness(tmp_path):
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    old = _card("t_legacy", CHAT, status="blocked", at=now - 61 * 3600)
    new = _card("t_legacy", CHAT, status="running", at=now)
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)",
                     ("default/t_legacy", json.dumps(old["task"])))
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,2)", (new["key"], json.dumps(new["task"])))
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []


def test_completion_notice_fallback_catches_up_after_baseline(tmp_path):
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    card = _card("t_fallback", CHAT, status="done", at=now - 8 * 3600)
    del card["task"]["completed_at"]
    card["task"]["completion_notice"]["created_at"] = now - 8 * 3600
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)", (card["key"], json.dumps(card["task"])))
    assert len(krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)) == 1
