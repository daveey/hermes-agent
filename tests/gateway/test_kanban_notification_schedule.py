"""Binding churn and refusal floors never replace the action's custody."""
import builtins
import sqlite3

from gateway.kanban_notification_schedule import BoundActions, PassReport
from hermes_cli.fact_contract import Fact, FactKey
from hermes_cli.fact_fake import FakeStore
from tests.tui_gateway import test_notification_facts as helpers
from tui_gateway import notification_facts as facts


def test_binding_churn_quiet_lookup_and_retry_floor(monkeypatch):
    store = FakeStore()
    helpers.ready_siblings(store)
    owner = facts.bind(store, helpers.SCOPE, "/canonical/gateway")
    gate = BoundActions(lambda: None)
    owner.report_ready()
    store.activate(helpers.SCOPE)
    store.drain()
    try:
        assert gate.due(0, legacy_due=True)
        report = PassReport(pending=True)
        gate.settled(report, 0, 5)
        store.publish([Fact(FactKey(helpers.SCOPE, "SourceState", "board"), 1)])
        store.drain()
        assert not gate.due(1, legacy_due=True)
        assert gate.due(5, legacy_due=False)
        gate.settled(PassReport(), 5, 5)

        def forbidden(*args, **kwargs):
            raise AssertionError("quiet scheduling performed source I/O")

        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", forbidden)
            patch.setattr(sqlite3, "connect", forbidden)
            assert not gate.due(100, legacy_due=True)
        owner.close()
        assert not gate.due(101, legacy_due=True)  # missing binding is not rollback
        replacement = facts.bind(store, helpers.SCOPE, "/canonical/gateway")
        try:
            # Explicit legacy mode of the successor restores the existing cadence.
            store.rollback(helpers.SCOPE)
            assert gate.due(102, legacy_due=True)
        finally:
            replacement.close()
    finally:
        gate.close()
        owner.close()
