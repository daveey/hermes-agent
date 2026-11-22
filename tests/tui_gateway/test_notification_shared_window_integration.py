"""Real SQLite arrival and clock boundaries for the poller's bounded structural view."""
import json
import sqlite3

import pytest

from tests.tui_gateway.test_kanban_remote_arrival import _hub, _card, CHAT
from tui_gateway import kanban_remote_notices as notices
from tui_gateway import notification_shared_window as shared


@pytest.mark.parametrize("enabled", [True, False])
def test_real_arrival_visible_after_window_or_immediately_when_off(tmp_path, monkeypatch, enabled):
    notices._reset_for_tests()
    monkeypatch.setenv("HERMES_NOTIFICATION_SHARED_WINDOW", "1" if enabled else "0")
    clock = [100.0]
    monkeypatch.setattr(shared, "_clock", lambda: clock[0])
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    kwargs = dict(hub_db=replica, ledger=ledger, now=now, shared_window=True)
    try:
        assert notices.collect(CHAT, "default", **kwargs) == []  # real observation baseline
        card = _card("t_arrived", CHAT, status="done", at=now)
        with sqlite3.connect(replica) as conn:
            conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)",
                         (card["key"], json.dumps(card["task"])))
        clock[0] = 101.0
        early = notices.collect(CHAT, "default", **kwargs)
        assert len(early) == (0 if enabled else 1)
        clock[0] = 102.0
        found = notices.collect(CHAT, "default", **kwargs) if enabled else early
        assert len(found) == 1 and "t_arrived" in found[0]["ref"]
        assert notices.ack(found[0], ledger=ledger)
        assert notices.collect(CHAT, "default", **kwargs) == []
    finally:
        notices._reset_for_tests()


def test_real_clock_eligibility_not_frozen_with_candidates(tmp_path, monkeypatch):
    notices._reset_for_tests()
    monkeypatch.delenv("HERMES_NOTIFICATION_SHARED_WINDOW", raising=False)
    monkeypatch.setattr(shared, "_clock", lambda: 100.0)
    now = 1_800_000_000.0
    replica = _hub(tmp_path, [_card("t_boundary", CHAT, status="blocked", at=now)])
    ledger = tmp_path / "ledger.db"
    kwargs = dict(hub_db=replica, ledger=ledger, shared_window=True)
    try:
        found = notices.collect(CHAT, "default", now=now, **kwargs)
        assert len(found) == 1
        assert notices.release(found[0], ledger=ledger)
        # Same structural window, but freshness is recomputed against this tick's clock.
        assert notices.collect(CHAT, "default", now=now + notices.LATE_MAX_S + 1, **kwargs) == []
        with sqlite3.connect(ledger) as conn:
            assert conn.execute("SELECT delivered FROM notices").fetchone()[0] == notices.SETTLED
    finally:
        notices._reset_for_tests()
