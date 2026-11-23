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
    owner.step(2.5, reconcile=False)
    assert not owner.ready
    owner.step(3, reconcile=False)
    assert owner.ready
    assert action.due(legacy_due=False)


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
