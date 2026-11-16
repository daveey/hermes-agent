"""Behavioural holder pins for #3125; exact and narrow verdicts are independent."""
from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_narrow as narrow
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_holder_narrow as helpers

world = helpers.world
gate = helpers.gate
SCOPE = helpers.SCOPE


def test_narrow_move_refreshes_even_when_exact_key_stays(world, gate):
    gate['key'] = 'K2'
    helpers.helpers._advance(world, 10)
    assert host._holder_refresh_scope(SCOPE)
    assert helpers._header()['narrow_key'] == 'K2'


def test_recent_confirmation_is_not_written_again(world, gate, monkeypatch):
    helpers.helpers._advance(world, 10)
    assert not host._holder_refresh_scope(SCOPE)
    confirmed = helpers._header()['verified_at']
    calls = []
    original = disk.confirm_host

    def confirm(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(disk, 'confirm_host', confirm)
    helpers.helpers._advance(world, 2)
    assert not host._holder_refresh_scope(SCOPE)
    assert calls == []
    assert helpers._header()['verified_at'] == confirmed
    helpers.helpers._advance(world, 4)
    assert not host._holder_refresh_scope(SCOPE)
    assert calls == []  # [daveey] nothing watched moved since the confirmation: nothing to re-stamp
    helpers._move(world)  # a watched file moved, the narrow key did not: confirmed again (6 s since the last)
    assert not host._holder_refresh_scope(SCOPE)
    assert len(calls) == 1
    helpers.helpers._advance(world, 2)
    assert not host._holder_refresh_scope(SCOPE)
    assert len(calls) == 1  # confirmed 2 s ago and the signature still matches it


def test_failed_exact_fingerprint_does_not_compute_narrow(world, monkeypatch):
    calls = []
    monkeypatch.setattr(host, 'fingerprint', lambda *args: None)
    monkeypatch.setattr(host._holder_ctx, 'active', True, raising=False)
    monkeypatch.setattr(host, '_narrow_now', lambda *args: calls.append(args) or 'unsafe')
    try:
        prepared = host.prepare(SCOPE)
        assert prepared is not None
        assert prepared[2] is None and prepared[5] is None
        assert calls == []
    finally:
        host.finish(SCOPE)


def test_waiting_identity_failure_is_unknown(world, monkeypatch):
    def broken():
        raise OSError('identity unavailable')

    monkeypatch.setattr(host, 'waiting_identity', broken)
    monkeypatch.setattr(narrow, 'key', lambda *args: 'otherwise-valid')
    assert host._narrow_now(SCOPE, ()) is None


def test_activity_pin_is_used_instead_of_new_reads(world, monkeypatch):
    pin = ('pinned-full',)
    monkeypatch.setattr(ksa._revalidate.inputs_pin, 'fingerprints', ((), pin), raising=False)
    monkeypatch.setattr(ksa, '_activity_fingerprint', lambda **kwargs: ('new-local',))
    monkeypatch.setattr(ksa, '_shadow_fingerprint', lambda: ('new-shadow',))
    assert host._activity_now() == (pin, world['wall'])
    monkeypatch.setattr(ksa._revalidate.inputs_pin, 'fingerprints', ((), None))
    assert host._activity_now() == (('new-local', 'new-shadow'), world['wall'])
