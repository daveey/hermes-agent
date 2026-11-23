"""Transcript commit and notice ACK have independent retry custody."""
from types import SimpleNamespace
import time

import pytest

from tui_gateway import kanban_remote_notices as krn, server
from tests.tui_gateway import test_kanban_remote_notices as fixtures


@pytest.mark.parametrize("failure", ["false", "raise"])
def test_remote_ack_failure_keeps_durable_claim_for_claims_only_pass(tmp_path, monkeypatch, failure):
    now = time.time()
    replica = fixtures._hub(tmp_path, [fixtures._card("t_ack_retry", fixtures.CHAT,
        status="done", at=now - 4, final="committed notice")])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    monkeypatch.setattr(krn, "ledger_path", lambda: ledger)
    remote = krn.collect(fixtures.CHAT, "default", hub_db=replica, ledger=ledger, now=now)[0]
    text = server._KanbanText(remote["text"])
    claim = {"remote": remote, "texts": [text]}
    session = {"_kanban_claims": [claim], "_kanban_pending": []}
    real_ack = krn.ack

    def fail_ack(_notice):
        if failure == "raise":
            raise OSError("injected ACK failure after transcript commit")
        return False

    from hermes_state import SessionDB
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(fixtures.CHAT, "desktop")
        db.record_kanban_notice(fixtures.CHAT, str(text),
            {"notice_id": "ack-retry", "kanban_event_keys": [remote["event_key"]]})
        before = db.get_messages(fixtures.CHAT)
    monkeypatch.setattr(krn, "ack", fail_ack)
    server._kb_ack_durable(session, {id(text)})
    server._kb_mark_delivered(session, [text])
    assert session["_kanban_claims"] == [claim]
    assert claim["durable"] and not claim.get("acked")
    assert fixtures._ledger_row(ledger, fixtures.CHAT, remote["ref"])["delivered"] == 0

    # No later notice, no new turn: the next executor pass owes only an ACK.
    monkeypatch.setattr(krn, "ack", real_ack)
    monkeypatch.setattr(server, "_kb_settle_held", lambda _s: None)
    monkeypatch.setattr(server, "_notif_claim_turn", lambda _s: pytest.fail("ACK retry started a turn"))
    server._notif_poll_kanban_scoped("ui", session, ([], []))
    assert fixtures._ledger_row(ledger, fixtures.CHAT, remote["ref"])["delivered"] == 1
    assert session["_kanban_claims"] == []
    with SessionDB(tmp_path / "state.db") as db:
        assert db.get_messages(fixtures.CHAT) == before
        assert len(before) == 1


@pytest.mark.parametrize("earlier_in_flight", [True, False])
def test_local_ack_false_requires_marker_readback(monkeypatch, earlier_in_flight):
    from hermes_cli import kanban_db_connect as kbc, kanban_db_notify as kbn
    claim = {"board": "default", "task_id": "t_ack", "platform": "tui", "chat_id": "chat",
             "old": 5, "new": 9, "texts": [], "durable": True}
    session = {"_kanban_claims": [claim]}
    monkeypatch.setattr(kbc, "connect", lambda **_kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kbn, "ack_notify_claim", lambda *_a, **_kw: False)
    monkeypatch.setattr(kbn, "notify_claim_in_flight", lambda *_a, **_kw: earlier_in_flight)
    server._kb_ack_durable(session, set())
    assert bool(claim.get("acked")) is not earlier_in_flight


def test_expiring_undelivered_claim_preserves_committed_ack_obligation(monkeypatch):
    from tui_gateway import kanban_claim_custody as custody
    committed = {"remote": {"ref": "committed"}, "texts": [], "durable": True}
    expired = {"remote": {"ref": "expired"}, "texts": ["pending"], "claimed_mono": 0}
    session = {"_kanban_claims": [committed, expired], "_kanban_pending": ["pending"]}
    released = []
    monkeypatch.setattr(krn, "release", lambda notice: released.append(notice) or True)
    assert custody.release_expired(session, custody.NOTIFY_CLAIM_STALE_S + 1)
    assert released == [expired["remote"]]
    assert session["_kanban_claims"] == [committed]
    assert session["_kanban_pending"] == []


@pytest.mark.parametrize("not_owned", [True, False])
def test_refused_turn_retains_remote_committed_ack(monkeypatch, not_owned):
    import threading
    from tui_gateway import kanban_consumer_health
    claim = {"remote": {"ref": "committed"}, "texts": [], "durable": True}
    session = {"_kanban_claims": [claim], "history_lock": threading.Lock()}
    monkeypatch.setattr(kanban_consumer_health, "invalidate", lambda _s: None)
    server._kb_rewind_refused(session, [], not_owned=not_owned)
    assert session["_kanban_claims"] == [claim]
