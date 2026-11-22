"""Waiting providers declare event coverage independently of cache keys."""
from pathlib import Path
import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa


def test_discovery_covers_absent_configured_sources(monkeypatch, tmp_path):
    from hermes_cli import kanban_db
    from hermes_constants import get_hermes_home

    root = tmp_path / 'shared-boards'
    hub = tmp_path / 'hub-state'
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(root))
    monkeypatch.setenv('HERMES_HUB_STATE', str(hub))
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(tmp_path / 'mail.json'))
    files, directories = host._discovery_paths()
    assert root / 'kanban.db' in files
    assert hub / 'hub.db' in files
    assert kanban_db.boards_root() in directories
    assert root / 'kanban' / 'sync-shadow' in directories
    assert get_hermes_home() / 'config.yaml' in files
    assert tmp_path / 'mail.json' in files
    # An existing empty board directory must be watched for later DB creation.
    board = kanban_db.boards_root() / 'new'
    board.mkdir(parents=True)
    _, directories = host._discovery_paths()
    assert board in directories


def test_configured_source_creation_notifies_native_holder(monkeypatch, tmp_path):
    from hermes_cli.annotation_file_events import FileEvents

    root = tmp_path / 'boards-root'
    root.mkdir()
    hub = tmp_path / 'hub-root'
    hub.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(root))
    monkeypatch.setenv('HERMES_HUB_STATE', str(hub))
    with FileEvents() as events:
        files, directories = host._discovery_paths()
        events.replace(files, directories=directories)
        (hub / 'hub.db').write_bytes(b'new')
        assert events.wait(0.5)
        events.replace(files, directories=directories)
        (root / 'kanban.db').write_bytes(b'new')
        assert events.wait(0.5)


def test_declared_source_has_no_periodic_deadline(monkeypatch, tmp_path):
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, '_event_sources', {})
    fn = dict
    ksa.register_waiting_source('events', fn, watch_paths=lambda: [tmp_path / 'state.db'],
                               next_deadline=lambda: None)
    assert host.waiting_events(100) == ({tmp_path / 'state.db'}, None)


def test_unknown_provider_is_bounded_and_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, '_event_sources', {})
    monkeypatch.setattr(host, '_event_warned', set())
    ksa.register_waiting_source('legacy', dict)
    assert host.waiting_events(100) == (set(), 160)
    assert host.waiting_events(101) == (set(), 161)
    assert len([r for r in caplog.records if 'legacy' in r.message]) == 1


def test_replacement_removes_old_coverage(monkeypatch):
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, '_event_sources', {})
    ksa.register_waiting_source('events', dict, watch_paths=lambda: [Path('/old')])
    ksa.register_waiting_source('events', dict)
    assert host.waiting_events(100) == (set(), 160)
    ksa.register_waiting_source('events', None)
    assert host.waiting_events(100) == (set(), None)


def test_deadline_is_absolute_and_bad_declaration_retries(monkeypatch):
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, '_event_sources', {})
    ksa.register_waiting_source('clock', dict, watch_paths=list, next_deadline=lambda: 105)
    assert host.waiting_events(100) == (set(), 105)
    def broken():
        raise OSError('source unavailable')
    ksa.register_waiting_source('clock', dict, watch_paths=broken)
    assert host.waiting_events(100) == (set(), 102)


@pytest.mark.parametrize('due', [float('nan'), float('inf'), -float('inf'), 'bad', 99])
def test_invalid_or_unconsumed_deadline_has_bounded_retry(monkeypatch, due):
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, '_event_sources', {})
    ksa.register_waiting_source('clock', dict, watch_paths=list, next_deadline=lambda: due)
    assert host.waiting_events(100) == (set(), 102)
