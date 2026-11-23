"""Process-wide native watch leases retain source and callback isolation."""
import contextvars
import os
import threading

import pytest

from hermes_constants import (
    get_hermes_home, reset_hermes_home_override, set_hermes_home_override,
)
from hermes_cli import fact_source_watch as native
from hermes_cli.fact_source_signal import cue_path
from hermes_cli.fact_source_watch import NativeSourceWatch


@pytest.mark.platforms('linux')
def test_many_profiles_share_one_native_thread_and_keep_callback_context(tmp_path):
    profile = contextvars.ContextVar('watch_profile', default='unbound')
    watches = []
    hints = [threading.Event() for _ in range(11)]
    seen = [[] for _ in hints]
    baseline = {t.ident for t in threading.enumerate() if t.name == 'fact-native-watch'}
    try:
        for index, hint in enumerate(hints):
            path = tmp_path / str(index) / 'state.db'
            path.parent.mkdir()
            path.write_text('bootstrap')
            token = profile.set(str(index))
            home_token = set_hermes_home_override(path.parent)
            try:
                def wake(index=index, hint=hint):
                    seen[index].append((profile.get(), get_hermes_home()))
                    hint.set()
                watches.append(NativeSourceWatch((path,), wake))
            finally:
                reset_hermes_home_override(home_token)
                profile.reset(token)
        threads = [t for t in threading.enumerate()
                   if t.name == 'fact-native-watch' and t.ident not in baseline]
        assert len(threads) == 1
        # No command/registration wake is allowed to become a source hint.
        assert not any(hint.is_set() for hint in hints)
        for index, hint in enumerate(hints):
            (tmp_path / str(index) / 'state.db').write_text('changed')
            assert hint.wait(5)
            assert set(seen[index]) == {(str(index), tmp_path / str(index))}
            # Barrier after this readiness batch, not a timing-based negative
            # assertion: a union-path broadcast would have run siblings by now.
            watches[index]._owner.submit(lambda: None).wait()
            assert not any(future.is_set() for future in hints[index + 1:])
        watches[0].close()
        watches.pop(0)
        hints[-1].clear()
        (tmp_path / '10' / 'state.db').write_text('survives sibling close')
        assert hints[-1].wait(5)
    finally:
        for watch in watches:
            watch.close()
    assert not [t for t in threading.enumerate()
                if t.name == 'fact-native-watch' and t.ident not in baseline]


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('operation', ['replace', 'wait'])
def test_rearm_error_is_reported_then_repaired_in_subscriber_context(
        tmp_path, monkeypatch, operation):
    path = tmp_path / 'source.db'
    path.write_text('initial')
    failed = threading.Event()
    repaired = threading.Event()
    armed = threading.Event()
    original = getattr(native.FileEvents, operation)
    watch = None

    def fail_once(events, *args, **kwargs):
        if armed.is_set() and events is watch._events:
            armed.clear()
            raise OSError('injected rearm failure')
        return original(events, *args, **kwargs)

    def wake():
        try:
            watch.check()
        except OSError:
            failed.set()
        else:
            repaired.set()

    watch = NativeSourceWatch((path,), wake)
    monkeypatch.setattr(native.FileEvents, operation, fail_once)
    try:
        armed.set()
        cue_path(path).write_text('restore hint')
        assert failed.wait(5)
        assert repaired.wait(5)
        watch.check()
    finally:
        watch.close()


@pytest.mark.platforms('linux')
def test_failed_close_keeps_lease_and_descriptors_for_retry(tmp_path, monkeypatch):
    path = tmp_path / 'source.db'
    hint = threading.Event()
    watch = NativeSourceWatch((path,), hint.set)
    owner = watch._owner
    descriptor = watch._events._fd
    original = native.FileEvents.close
    fail = True

    def close(events):
        nonlocal fail
        if events is watch._events and fail:
            fail = False
            raise OSError('injected close failure')
        return original(events)

    monkeypatch.setattr(native.FileEvents, 'close', close)
    try:
        with pytest.raises(OSError, match='injected close failure'):
            watch.close()
        os.fstat(descriptor)
        assert native._shared is owner
        path.write_text('still covered')
        assert hint.wait(5)
    finally:
        watch.close()
    assert not owner._thread.is_alive()
    assert native._shared is None


@pytest.mark.platforms('linux')
def test_failed_initial_registration_releases_empty_process_owner(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('injected registration failure')

    with monkeypatch.context() as patch:
        patch.setattr(native.FileEvents, 'replace', fail)
        with pytest.raises(OSError, match='injected registration failure'):
            NativeSourceWatch((tmp_path / 'source.db',), lambda: None)
    assert native._shared is None
    watch = NativeSourceWatch((tmp_path / 'source.db',), lambda: None)
    watch.close()


@pytest.mark.platforms('linux')
def test_join_timeout_retains_owner_and_close_can_retry(tmp_path, monkeypatch):
    watch = NativeSourceWatch((tmp_path / 'source.db',), lambda: None)
    owner = watch._owner
    original = owner._thread.is_alive
    calls = 0

    def alive():
        nonlocal calls
        calls += 1
        return True if calls == 1 else original()

    monkeypatch.setattr(owner._thread, 'is_alive', alive)
    try:
        with pytest.raises(TimeoutError, match='descriptors retained'):
            watch.close()
        assert native._shared is owner
    finally:
        watch.close()
    assert native._shared is None


@pytest.mark.platforms('linux')
def test_directory_and_sqlite_sidecar_hints_are_armed_before_return(tmp_path):
    directory = tmp_path / 'children'
    directory.mkdir()
    path = tmp_path / 'source.db'
    hint = threading.Event()
    watch = NativeSourceWatch((path,), hint.set, directories=(directory,))
    try:
        (directory / 'new-child').write_text('created immediately')
        assert hint.wait(5)
        hint.clear()
        (tmp_path / 'source.db-wal').write_text('commit')
        assert hint.wait(5)
    finally:
        watch.close()
