"""Holder pacing and mutation generation regression controls. [daveey]"""
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_host as helpers

world = helpers.world
SCOPE = helpers.SCOPE


def test_moved_inputs_wait_for_floor_then_refresh(world):
    helpers._poll()
    host.demand(SCOPE)
    world['conn'].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    helpers._advance(world, 2)
    assert not host._holder_refresh_scope(SCOPE)
    helpers._advance(world, 8)
    assert host._holder_refresh_scope(SCOPE)
    assert ksa._last_annotation[SCOPE][0]['s1'] == 3


def test_own_mutation_bypasses_floor(world):
    helpers._poll()
    host.demand(SCOPE)
    world['conn'].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    helpers._advance(world, 2)
    host.kick(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    assert not host._holder_refresh_scope(SCOPE)


def test_failed_refresh_retains_kick(world, monkeypatch):
    helpers._poll()
    host.demand(SCOPE)
    host.kick(SCOPE)
    with ksa._annotate_refresh_lock:
        assert not host._holder_refresh_scope(SCOPE)
    assert host._holder_refresh_scope(SCOPE)


def test_kick_during_read_survives(world, monkeypatch):
    helpers._poll()
    host.demand(SCOPE)
    host.kick(SCOPE)
    original = ksa._refresh_annotation_locked
    def reading(scope):
        result = original(scope)
        host.kick(scope)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(ksa, '_refresh_annotation_locked', reading)
        assert host._holder_refresh_scope(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    assert not host._holder_refresh_scope(SCOPE)


def test_exception_retains_kick_and_releases_lock(world, monkeypatch):
    import pytest

    helpers._poll()
    host.demand(SCOPE)
    host.kick(SCOPE)
    def broken(scope):
        raise RuntimeError('read failed')
    with monkeypatch.context() as patch:
        patch.setattr(ksa, '_refresh_annotation_locked', broken)
        with pytest.raises(RuntimeError, match='read failed'):
            host._holder_refresh_scope(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    assert not host._holder_refresh_scope(SCOPE)


def test_other_scope_kick_does_not_bypass_floor(world):
    helpers._poll()
    host.demand(SCOPE)
    world['conn'].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    host.kick(('annotation-host-test', 'other'))
    helpers._advance(world, 2)
    assert not host._holder_refresh_scope(SCOPE)


def _cue_from_process(path, scope):
    from pathlib import Path
    from hermes_cli import kanban_annotation_mutation as mutation
    mutation.disk._path = lambda _scope: Path(path)
    mutation.record(scope)


def test_foreign_process_cue_bypasses_floor(world):
    import multiprocessing

    helpers._poll()
    host.demand(SCOPE)
    helpers._advance(world, 2)
    child = multiprocessing.get_context('spawn').Process(
        target=_cue_from_process, args=(str(host._disk._path(SCOPE)), SCOPE))
    child.start()
    try:
        child.join(20)
        assert child.exitcode == 0
        assert host._holder_refresh_scope(SCOPE)
        assert not host._holder_refresh_scope(SCOPE)
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)
        child.close()


def test_kick_survives_process_cache_reset(world):
    helpers._poll()
    host.kick(SCOPE)
    helpers._advance(world, 2)
    host.reset()
    assert host._holder_refresh_scope(SCOPE)
    host.reset()
    assert not host._holder_refresh_scope(SCOPE)


def test_failed_publication_does_not_acknowledge_kick(world, monkeypatch):
    helpers._poll()
    host.kick(SCOPE)
    helpers._advance(world, 2)
    with monkeypatch.context() as patch:
        patch.setattr(host._disk, 'save_host', lambda *a, **kw: False)
        assert host._holder_refresh_scope(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    assert not host._holder_refresh_scope(SCOPE)


def test_ordinary_publication_cannot_acknowledge_potentially_old_counts(world):
    helpers._poll()
    host.kick(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    # A different poller may reuse counts read before the cue. Its publication
    # must not inherit an acknowledgement from a newer holder publication.
    with ksa._annotate_refresh_lock:
        ksa._refresh_annotation_locked(SCOPE)
    assert host._holder_refresh_scope(SCOPE)
    assert not host._holder_refresh_scope(SCOPE)


def test_renew_remains_twenty_seconds(world):
    helpers._poll()
    host.demand(SCOPE)
    helpers._advance(world, 19)
    assert not host._holder_refresh_scope(SCOPE)
    helpers._advance(world, 1)
    assert host._holder_refresh_scope(SCOPE)
