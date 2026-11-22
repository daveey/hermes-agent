"""Done-check inputs must not disappear when annotation timers are removed."""
import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_session_activity_attention import run_checks


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(ksa, '_done_checks', {})
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, '_event_sources', {})
    monkeypatch.setattr(host, '_done_event_sources', {}, raising=False)
    monkeypatch.setattr(host, '_event_warned', set())
    monkeypatch.setattr(ksa, '_attention', ksa._AttentionCache(30))


def test_legacy_done_check_has_bounded_fallback(caplog):
    ksa.register_done_check('legacy', lambda card: False)
    assert host.waiting_events(100) == (set(), 160)
    assert host.waiting_events(101) == (set(), 161)
    assert len([r for r in caplog.records if 'legacy' in r.message]) == 1


def test_done_check_registration_and_disposal_wake_holder(monkeypatch):
    wakes = []
    monkeypatch.setattr(host, 'wake_holder', lambda: wakes.append(True))
    dispose = ksa.register_done_check('check', lambda card: False)
    assert len(wakes) == 1
    dispose()
    assert len(wakes) == 2
    dispose()
    assert len(wakes) == 2


def test_done_check_events_are_separate_from_same_named_waiting_source(tmp_path):
    ksa.register_waiting_source('same', dict, watch_paths=lambda: [tmp_path / 'waiting.db'])
    dispose = ksa.register_done_check('same', lambda card: False,
                                      watch_paths=lambda: [tmp_path / 'done.db'], next_deadline=lambda: 105)
    assert host.waiting_events(100) == ({tmp_path / 'waiting.db', tmp_path / 'done.db'}, 105)
    dispose()
    assert host.waiting_events(100) == ({tmp_path / 'waiting.db'}, None)


def test_old_disposer_cannot_remove_replacement_coverage(tmp_path):
    dispose = ksa.register_done_check('same', lambda card: False,
                                      watch_paths=lambda: [tmp_path / 'old.db'])
    ksa.register_done_check('same', lambda card: True, watch_paths=lambda: [tmp_path / 'new.db'])
    dispose()
    assert host.waiting_events(100) == ({tmp_path / 'new.db'}, None)


def test_failed_done_check_retries_without_another_input_write(monkeypatch):
    due = []
    monkeypatch.setattr(host, 'note_deadline', due.append)
    monkeypatch.setattr(host.time, 'time', lambda: 100)
    def broken(card):
        raise OSError('temporary external input failure')
    assert run_checks([broken], {'id': 'done'}) is False
    assert due == [100 + host.HOLDER_INTERVAL_S]


def test_declared_done_check_failure_keeps_recovery_bounded():
    def broken():
        raise OSError('input discovery unavailable')
    ksa.register_done_check('check', lambda card: False, watch_paths=broken)
    assert host.waiting_events(100) == (set(), 102)
