"""Binding churn and refusal floors never replace the action's custody."""
import builtins
import sqlite3

from hermes_cli import fact_links_health as links_health

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


def test_links_recovery_admits_existing_executor_without_unrelated_write(monkeypatch):
    links_health.reset_for_tests()
    store = FakeStore()
    helpers.ready_siblings(store)
    owner = facts.bind(store, helpers.SCOPE, '/canonical/gateway')
    gate = BoundActions(lambda: None)
    owner.report_ready()
    store.activate(helpers.SCOPE)
    store.drain()
    try:
        assert gate.due(0, legacy_due=True)
        gate.settled(PassReport(), 0, 60)
        assert not gate.due(1, legacy_due=False)
        links_health.changed(('/foreign', 'other', 'boards'), False)
        assert not gate.due(2, legacy_due=False)
        def forbidden(*a, **kw):
            raise AssertionError('health scheduling did source I/O')
        with monkeypatch.context() as patch:
            patch.setattr(builtins, 'open', forbidden)
            patch.setattr(sqlite3, 'connect', forbidden)
            links_health.changed(('/canonical/gateway', 'default', 'boards'), False)
            assert gate.due(3, legacy_due=False)
            gate.settled(PassReport(pending=True), 3, 60)
            links_health.changed(('/canonical/gateway', 'default', 'boards'), True)
            assert not gate.due(4, legacy_due=False)  # retain refusal floor, not extra polling
            assert gate.due(63, legacy_due=False)
            gate.settled(PassReport(), 63, 60)
            assert not gate.due(64, legacy_due=False)
    finally:
        gate.close()
        owner.close()
        links_health.reset_for_tests()
