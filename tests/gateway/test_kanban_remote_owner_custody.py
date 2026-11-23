"""Claims-only settlement must survive a false durable ACK with no new fact."""
from gateway import kanban_remote_owner as owner
from tui_gateway import kanban_remote_notices as notices
from tests.gateway import test_kanban_remote_owner_delivery as helpers


env = helpers.env
TID = "t_01a1cfabf44499dd"


def test_false_ack_retains_committed_claim_until_retry(env, monkeypatch):
    hub, runner, authority, fifo, db = env
    authority.sessions[helpers.CHAT] = helpers._Live(helpers.CHAT)
    helpers._busy(db, fifo)
    helpers._publish(hub, [helpers._done(TID)])
    assert helpers._tick(runner)
    fifo.boundary()
    authority.sessions.clear()  # claims-only: do not let collect's independent ACK mask this path
    real_ack = notices.ack
    monkeypatch.setattr(notices, "ack", lambda *a, **kw: False)
    helpers._tick(runner)
    assert owner._admitted, "failed ACK must not discard the only claims-only retry"
    assert helpers._ledger(TID)["delivered"] == 0
    # No active session, no new source event: custody alone must settle the ACK.
    authority.sessions.clear()
    monkeypatch.setattr(notices, "ack", real_ack)
    helpers._tick(runner)
    assert not owner._admitted
    assert helpers._ledger(TID)["delivered"] == notices.DELIVERED
    assert len(helpers._rows(helpers._key(TID))) == 1
    assert fifo.queued() == []


def test_failed_release_retains_custody_but_never_deletes_foreign_claim(env, monkeypatch):
    import sqlite3
    hub, runner, authority, fifo, db = env
    authority.sessions[helpers.CHAT] = helpers._Live(helpers.CHAT)
    helpers._busy(db, fifo)
    helpers._publish(hub, [helpers._done(TID)])
    [item] = notices.collect(helpers.CHAT, "default", per_link=True)
    real_ledger = notices._ledger

    def unavailable(*a, **kw):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(notices, "_ledger", unavailable)
    owner._release_tracked([item], frozenset({helpers.CHAT}), helpers.CHAT)
    assert owner._admitted
    monkeypatch.setattr(notices, "_ledger", real_ledger)
    # Replacement holder is data only; never probe or signal a fabricated PID.
    with sqlite3.connect(notices.ledger_path()) as conn:
        conn.execute("UPDATE notices SET holder_pid=-1, holder_start=42 WHERE delivered=0")
    assert owner._release_claim(item)
    assert helpers._ledger(TID)["holder_pid"] == -1
    with sqlite3.connect(notices.ledger_path()) as conn:
        conn.execute("UPDATE notices SET holder_pid=?, holder_start=? WHERE delivered=0", notices._my_holder())
    authority.sessions.clear()
    helpers._tick(runner)
    assert not owner._admitted
    assert helpers._ledger(TID) is None


def test_committed_ack_retries_through_fact_rollback_without_duplicate(env, monkeypatch):
    from gateway.kanban_notification_schedule import BoundActions, PassReport
    from hermes_cli.fact_fake import FakeStore
    from tests.tui_gateway import test_notification_facts as facts_helpers
    from tui_gateway import notification_facts
    import asyncio

    hub, runner, authority, fifo, db = env
    authority.sessions[helpers.CHAT] = helpers._Live(helpers.CHAT)
    helpers._busy(db, fifo)
    helpers._publish(hub, [helpers._done(TID)])
    store = FakeStore()
    facts_helpers.ready_siblings(store)
    binding = notification_facts.bind(store, facts_helpers.SCOPE, "/canonical/remote-custody")
    binding.report_ready()
    store.activate(facts_helpers.SCOPE)
    store.drain()
    gate = BoundActions(lambda: None)

    def step(now):
        if gate.due(now, legacy_due=True):
            report = PassReport()
            result = asyncio.run(owner.tick(runner, report=report))
            gate.settled(report, now, 10)
            return result
        return []

    try:
        assert len(step(0)) == 1
        fifo.boundary()
        authority.sessions.clear()
        real_ack = notices.ack
        monkeypatch.setattr(notices, "ack", lambda *a, **kw: False)
        assert step(10) == [] and owner._admitted
        store.rollback(facts_helpers.SCOPE)
        assert step(11) == [] and owner._admitted  # rollback cannot bypass retry floor
        monkeypatch.setattr(notices, "ack", real_ack)
        assert step(20) == []
        assert not owner._admitted
        assert helpers._ledger(TID)["delivered"] == notices.DELIVERED
        assert step(30) == []
        assert len(helpers._rows(helpers._key(TID))) == 1
        assert fifo.queued() == []
    finally:
        gate.close()
        binding.close()


def test_written_claim_failed_ack_is_retained_without_readmission(env, monkeypatch):
    hub, runner, authority, fifo, db = env
    authority.sessions[helpers.CHAT] = helpers._Live(helpers.CHAT)
    helpers._busy(db, fifo)
    helpers._publish(hub, [helpers._done(TID)])
    [item] = notices.collect(helpers.CHAT, "default", per_link=True)
    helpers._write_notice_row(db, helpers.CHAT, helpers._key(TID))
    real_ack = notices.ack
    monkeypatch.setattr(notices, "ack", lambda *a, **kw: False)
    assert owner._drop_written(item, lambda *_: True, frozenset({helpers.CHAT}), helpers.CHAT) is None
    assert owner._admitted
    authority.sessions.clear()
    monkeypatch.setattr(notices, "ack", real_ack)
    helpers._tick(runner)
    assert not owner._admitted
    assert helpers._ledger(TID)["delivered"] == notices.DELIVERED
    assert len(helpers._rows(helpers._key(TID))) == 1
    assert fifo.queued() == []
