"""Catch-up must defer partial feeds; unchanged polls must not repeat observation work."""
import json
import sqlite3

import pytest

from tests.tui_gateway.test_kanban_remote_arrival import CHAT, _card, _hub
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_remote_observations as obs


def test_partial_catchup_on_established_host_is_deferred_not_settled(tmp_path):
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now - 8 * 3600) == []
    card = _card("t_partial_arrival", CHAT, status="done", at=now - 8 * 3600)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='101' WHERE k='head'")
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,101)", (card["key"], json.dumps(card["task"])))
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='101' WHERE k='cursor'")
    assert len(krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + 1)) == 1


def test_unchanged_snapshot_avoids_decode_and_ledger_work(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    card = _card("t_cached", CHAT, status="done", at=now)
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    first = obs.observe(replica, ledger, now)

    def forbidden(*args, **kwargs):
        pytest.fail("unchanged snapshot decoded rows or opened the observation ledger")

    monkeypatch.setattr(krn, "_task_rows", forbidden)
    monkeypatch.setattr(obs, "_persist", forbidden)
    assert obs.observe(replica, ledger, now + krn.FRESH_S + 1) == first


def test_cached_first_seen_still_ages_out(tmp_path):
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    card = _card("t_expiring", CHAT, status="done", at=now - 8 * 3600)
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)", (card["key"], json.dumps(card["task"])))
    first = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    assert len(first) == 1
    assert krn.release(first[0], ledger=ledger)
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + krn.FRESH_S + 1) == []
