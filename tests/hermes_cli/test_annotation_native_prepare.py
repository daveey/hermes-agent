"""Native event refresh does not build an unused legacy narrow certificate."""
import contextvars
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_host as helpers

world = helpers.world
SCOPE = helpers.SCOPE


def native(monkeypatch):
    monkeypatch.setattr(host, '_known', {SCOPE: contextvars.copy_context()})
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'SETTLE_S', ())
    disk.touch_demand(SCOPE)
    return host._event_refresh(SimpleNamespace(replace=lambda *a, **kw: None, control_only=False))


def header():
    out = {}
    copy = disk.load_host(SCOPE, shared=True, header_out=out)
    assert copy is not None
    return copy, out


def test_native_reads_and_committed_changes_never_probe_narrow(world, monkeypatch):
    monkeypatch.setattr(host, '_narrow_now', lambda *a: pytest.fail('native performed legacy inventory work'))
    native(monkeypatch)
    first, meta = header()
    assert first[2][0]['s1'] == 2
    assert meta.get('narrow_key') is None
    assert meta.get('watch_sig') is None
    world['conn'].execute("UPDATE tasks SET status='done' WHERE id='t1'")
    helpers._advance(world, 1)
    native(monkeypatch)
    second, _ = header()
    assert second[2][0]['s1'] == 1
    assert second[0] != first[0]
    helpers._other_process()
    assert host.adopt(SCOPE) == second[2]


def test_native_payload_matches_legacy_publication(world, monkeypatch):
    monkeypatch.setattr(host, '_narrow_now', lambda *a: 'legacy-key')
    host.demand(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    before, before_header = header()
    assert before_header['narrow_key'] == 'legacy-key'
    native(monkeypatch)
    after, after_header = header()
    assert after == before
    assert after_header.get('narrow_key') is None
    assert after_header.get('watch_sig') is None
    assert after_header['read_at'] == before_header['read_at']


def test_native_copy_does_not_gain_quiet_watch_extension(world, monkeypatch):
    native(monkeypatch)
    helpers._advance(world, host.DISPLAY_MAX_AGE_S)
    helpers._other_process()
    assert host.adopt_display(SCOPE) is not None
    helpers._advance(world, 0.01)
    helpers._other_process()
    assert host.adopt_display(SCOPE) is None


def test_legacy_holder_keeps_narrow_after_native_read(world, monkeypatch):
    calls = []
    monkeypatch.setattr(host, '_narrow_now', lambda *a: calls.append(a) or 'legacy-key')
    native(monkeypatch)
    assert calls == []
    host.kick(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    assert len(calls) == 1
    assert header()[1]['narrow_key'] == 'legacy-key'
