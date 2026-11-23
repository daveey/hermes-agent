"""Native readiness and rollback retain the canonical source inventory scope."""
import threading

import pytest

from tui_gateway import notification_gateway_sources as sources


def test_inventory_keeps_notifier_pin(monkeypatch, tmp_path):
    from agent import delegation_context
    from hermes_cli import kanban_db
    from tui_gateway import kanban_unloaded_gates
    pin = tmp_path / 'outside-board-dirs.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(pin))
    monkeypatch.setattr(delegation_context, 'explicit_board_intent_is_pinned', lambda: False)
    monkeypatch.setattr(kanban_db, '_descendant_may_follow_explicit_board', lambda slug: False)
    monkeypatch.setattr(kanban_db, 'list_boards', lambda **kw: [{'slug': 'other'}])
    monkeypatch.setattr(kanban_unloaded_gates, '_profile_paths', tuple)
    assert kanban_db.kanban_db_path('other') != pin  # unfenced caller is the precondition
    paths, _ = sources.inventory()
    assert pin in paths


class Watch:
    def __init__(self, *args, **kwargs):
        self.closed = False
        self.failed = False
        self.checks = 0

    def check(self):
        self.checks += 1
        if self.failed:
            raise OSError('watch backend interrupted')

    def close(self):
        self.closed = True


@pytest.fixture
def owner(monkeypatch, tmp_path):
    from hermes_cli import fact_source_watch
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    monkeypatch.setattr(sources, 'inventory', lambda: ((tmp_path / 'board.db',), ()))
    value = sources.Sources(threading.Event())
    yield value
    value.close()


def test_hint_failure_revokes_readiness_then_quiet_retry(owner):
    action = owner.add(lambda: None)
    owner.step(0, reconcile=True)
    assert owner.ready
    owner.watch.failed = True
    owner.changed()
    owner.step(2, reconcile=False)
    assert not owner.ready
    assert owner.retry_at == 3
    owner.watch.failed = False
    owner.changed()  # a healthy hint must not cancel the outstanding inventory/recovery check
    owner.step(2.5, reconcile=False)
    assert not owner.ready
    assert owner.retry_at == 3
    owner.step(3, reconcile=False)
    assert owner.ready
    assert action.due(legacy_due=False)


def test_sqlite_inventory_failure_revokes_readiness_and_recovers(owner, monkeypatch):
    import sqlite3
    action = owner.add(lambda: None)
    owner.step(0, reconcile=True)
    action.due(legacy_due=False)
    inventory = sources.inventory

    def unavailable():
        raise sqlite3.OperationalError('board roster unavailable')

    monkeypatch.setattr(sources, 'inventory', unavailable)
    owner.step(30, reconcile=True)
    assert not owner.ready
    assert owner.retry_at == 31
    assert action.due(legacy_due=False)
    monkeypatch.setattr(sources, 'inventory', inventory)
    owner.step(30.5, reconcile=False)
    assert not owner.ready
    owner.step(31, reconcile=False)
    assert owner.ready
    assert action.due(legacy_due=False)


def test_closed_event_loop_callback_does_not_starve_other_executors(owner):
    called = []

    def closed_loop():
        raise RuntimeError('Event loop is closed')

    failed = owner.add(closed_loop)
    healthy = owner.add(lambda: called.append('healthy'))
    owner.step(0, reconcile=True)
    assert called == ['healthy']
    assert failed.closed
    assert failed not in owner.actions
    assert healthy.due(legacy_due=False)


def test_last_consumer_off_withdraws_watch_and_on_rearms(owner):
    first = owner.add(lambda: None)
    owner.step(0, reconcile=True)
    old = owner.watch
    first.close()
    assert owner.event.is_set()
    owner.step(1, reconcile=False)
    assert old.closed
    assert owner.watch is None
    assert not owner.ready
    second = owner.add(lambda: None)
    assert not second.due(legacy_due=False)
    owner.step(2, reconcile=False)
    assert owner.ready
    assert owner.watch is not old
    assert second.due(legacy_due=False)


def test_unsupported_watch_uses_census_not_one_second_retries(owner, monkeypatch):
    from hermes_cli import fact_source_watch
    attempts = []

    def unsupported(*args, **kwargs):
        attempts.append(1)
        raise NotImplementedError

    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', unsupported)
    owner.add(lambda: None)
    owner.step(0, reconcile=True)
    owner.step(1, reconcile=False)
    assert attempts == [1]
    assert not owner.ready
    owner.step(30, reconcile=True)
    assert attempts == [1, 1]


def test_roster_census_arms_successor_before_closing_predecessor(owner, monkeypatch, tmp_path):
    from hermes_cli import fact_source_watch
    owner.add(lambda: None)
    owner.step(0, reconcile=True)
    old = owner.watch
    monkeypatch.setattr(sources, 'inventory', lambda: ((tmp_path / 'successor.db',), ()))
    fail = [True]

    def successor(*args, **kwargs):
        assert not old.closed
        if fail[0]:
            raise OSError('cannot yet arm successor scope')
        return Watch()

    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', successor)
    owner.step(30, reconcile=True)
    assert owner.watch is old
    assert not old.closed
    assert not owner.ready
    fail[0] = False
    owner.step(31, reconcile=False)
    assert old.closed
    assert owner.watch is not old
    assert owner.ready
    assert owner.signature == ((tmp_path / 'successor.db',), ())


def test_registration_context_survives_owner_thread_scope(monkeypatch, tmp_path):
    from contextvars import ContextVar
    from hermes_cli import fact_source_watch
    marker = ContextVar('native_registration_scope', default='outside')
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    monkeypatch.setattr(sources, 'inventory', lambda: ((tmp_path / (marker.get() + '.db'),), ()))
    token = marker.set('registered')
    owner = sources.Sources(threading.Event())
    marker.reset(token)
    try:
        owner.add(lambda: None)
        owner.step(0, reconcile=True)
        assert owner.signature == ((tmp_path / 'registered.db',), ())
        assert marker.get() == 'outside'
    finally:
        owner.close()


def test_native_gate_close_withdraws_membership(owner, monkeypatch):
    from gateway.kanban_notification_schedule import BoundActions
    from tui_gateway import notification_loaded_f

    plain = BoundActions(lambda: None)
    try:
        assert plain.legacy  # plain unbound callers are still legacy, independently of native rollback
    finally:
        plain.close()
    scheduler = notification_loaded_f.Scheduler(clock=lambda: 0)
    scheduler.gateway_sources = owner
    monkeypatch.setattr(notification_loaded_f, '_owner', scheduler)
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    gate = BoundActions(lambda: None, native=True)
    try:
        assert not gate.due(0, legacy_due=True)
        scheduler.step()
        assert owner.actions
        assert owner.ready
        gate.close()
        assert not owner.actions
        scheduler.step()
        assert not owner.ready
        assert owner.watch is None
    finally:
        gate.close()
        scheduler.close()


def test_native_to_bound_owner_never_reactivates_legacy_on_detach(owner, monkeypatch):
    from gateway.kanban_notification_schedule import BoundActions
    from hermes_cli.fact_fake import FakeStore
    from tests.tui_gateway import test_notification_facts as helpers
    from tui_gateway import notification_facts, notification_loaded_f

    scheduler = notification_loaded_f.Scheduler(clock=lambda: 0)
    scheduler.gateway_sources = owner
    monkeypatch.setattr(notification_loaded_f, '_owner', scheduler)
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    gate = BoundActions(lambda: None, native=True)
    store = FakeStore()
    helpers.ready_siblings(store)
    binding = None
    try:
        assert gate.legacy
        assert not gate.due(0, legacy_due=True)
        owner.step(0, reconcile=True)
        assert not gate.legacy
        binding = notification_facts.bind(store, helpers.SCOPE, '/canonical/native-transition')
        binding.report_ready()
        store.activate(helpers.SCOPE)
        store.drain()
        assert gate.due(0, legacy_due=True)
        assert gate.native_action is None
        assert not gate.legacy
        binding.close()
        assert not gate.due(0, legacy_due=True)
        assert not gate.legacy
        assert gate.native_action is None
    finally:
        gate.close()
        if binding is not None:
            binding.close()
        scheduler.close()
