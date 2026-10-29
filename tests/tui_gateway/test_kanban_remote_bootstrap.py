"""Bootstrap completeness and shared observations do not replay retained history."""
import contextlib
import json
import sqlite3

import pytest

from tests.tui_gateway.test_kanban_remote_arrival import CHAT, _card, _hub
from tui_gateway import kanban_remote_notices as krn


@contextlib.contextmanager
def _missing(profile, sid):
    yield None, None


@pytest.mark.parametrize("readiness", ["partial", "missing", "no_gateway"])
def test_partial_first_sync_cannot_seed_baseline(tmp_path, readiness):
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(replica) as conn:
        if readiness == "partial":
            conn.execute("UPDATE meta SET v='0' WHERE k='cursor'")
        elif readiness == "missing":
            conn.execute("DELETE FROM meta WHERE k IN ('cursor','head')")
        else:
            conn.execute("UPDATE meta SET v='' WHERE k='gateway'")
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=_missing,
                                    ledger=ledger, now=now) == []
    old = _card("t_late_history", CHAT, status="blocked", at=now - 5 * 86400)
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)", (old["key"], json.dumps(old["task"])))
        conn.execute("DELETE FROM meta")
        conn.executemany("INSERT INTO meta VALUES (?,?)", [("gateway", "macbook"), ("cursor", "100"), ("head", "100")])
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + 30) == []


def test_sweep_seeds_done_history_without_completion_evidence(tmp_path):
    now = 1_800_000_000.0
    card = _card("t_history_without_notice", CHAT, status="done", at=now - 5 * 86400)
    del card["task"]["completion_notice"]
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=_missing,
                                    ledger=ledger, now=now) == []
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + 30) == []


def test_new_chat_link_does_not_refresh_history(tmp_path):
    now = 1_800_000_000.0
    card = _card("t_unlinked_history", CHAT, status="done", at=now - 5 * 86400)
    links = card["task"].pop("sessions")
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    card["task"]["sessions"] = links
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET data=?,seq=2", (json.dumps(card["task"]),))
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + 30) == []
