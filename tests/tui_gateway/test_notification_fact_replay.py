"""The fact ring is a wake hint; real fanout receipts remain authoritative."""
import contextlib

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.fact_contract import Fact, FactKey
from hermes_cli.fact_fake import FakeStore
from tests.tui_gateway import test_notification_facts as facts
from tests.tui_gateway import test_session_outcome_notices as helpers
from tui_gateway import notice_fanout as fanout
from tui_gateway import notice_fanout_state as state
from tui_gateway.notification_facts import NotificationFacts


db = helpers.db


def test_overflow_and_rollback_preserve_real_action_cursor_and_receipts(db, monkeypatch):
    store = FakeStore(ring_revisions=1, subscriber_envelopes=1)
    facts.ready_siblings(store)
    owner = NotificationFacts(store, facts.SCOPE)
    action = owner.add_action(lambda: None)
    opener = helpers._opener(db)
    tid = helpers._card(done=False)
    with contextlib.closing(kbc.connect()) as conn:
        kb.block_task(conn, tid, reason="registry unavailable")
    helpers.ku.sweep_once(set(), now=helpers.LATER, session_db=opener)
    [notice] = helpers._notices(db)
    fanout.card_change_pass(session_db=opener)
    ident = str(kb.kanban_db_path().resolve())
    before = state.get_cursor(ident)
    assert before is not None
    owner.report_ready()
    try:
        store.activate(facts.SCOPE)
        store.drain()
        assert action.due(legacy_due=False)
        with contextlib.closing(kbc.connect()) as conn:
            kb.complete_task(conn, tid, summary="done")
        for revision in range(3):
            store.publish([Fact(FactKey(facts.SCOPE, "SourceState", "board/events"), revision)])
        store.drain()  # queue overflow/reset is a wake, not an action ACK
        state.forget_memory_for_tests()
        assert state.get_cursor(ident) == before
        assert helpers._notices(db) == [notice]
        assert action.due(legacy_due=False)
        with monkeypatch.context() as m:
            m.setattr(state, "set_cursor", lambda *args: False)
            fanout.card_change_pass(session_db=opener)  # commit action; crash before ACK
        [committed] = helpers._notices(db)
        assert committed["display_metadata"]["stale"] is True
        assert state.get_cursor(ident) == before
        store.rollback(facts.SCOPE)
        state.forget_memory_for_tests()  # successor executor / lease transfer
        assert state.get_cursor(ident) == before
        assert action.due(legacy_due=True)
        fanout.card_change_pass(session_db=opener)
        assert helpers._notices(db) == [committed]  # no duplicate row or metadata loss
        after = state.get_cursor(ident)
        assert after is not None and after > before
        assert fanout.card_change_pass(session_db=opener) == []
    finally:
        owner.close()
