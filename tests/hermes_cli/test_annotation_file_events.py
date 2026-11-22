"""Native notification boundary for annotation snapshots. [daveey]"""
import sys
import struct
import errno
import os

import pytest

from hermes_cli import annotation_file_events as module
from hermes_cli.annotation_file_events import FileEvents

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux native notification boundary')


def test_queue_overflow_invalidates_every_snapshot(tmp_path, monkeypatch):
    with FileEvents() as events:
        events.replace([tmp_path / 'input'])
        monkeypatch.setattr(events, '_readable', lambda timeout: {events._fd})
        reads = iter([struct.pack('iIII', -1, 0x4000, 0, 0)])

        def read(*args):
            try:
                return next(reads)
            except StopIteration:
                raise BlockingIOError from None

        monkeypatch.setattr(module.os, 'read', read)
        assert events.wait(0)
        assert not events.control_only

@pytest.mark.parametrize('mask', [0x400, 0x800, 0x2000, 0x8000])
def test_watch_loss_invalidates_snapshot(tmp_path, monkeypatch, mask):
    with FileEvents() as events:
        events.replace([tmp_path / 'input'])
        wd = next(iter(events._names))
        monkeypatch.setattr(events, '_readable', lambda timeout: {events._fd})
        reads = iter([struct.pack('iIII', wd, mask, 0, 0)])

        def read(*args):
            try:
                return next(reads)
            except StopIteration:
                raise BlockingIOError from None

        monkeypatch.setattr(module.os, 'read', read)
        assert events.wait(0)
        assert not events.control_only


def test_continuous_events_have_bounded_drain(tmp_path, monkeypatch):
    with FileEvents() as events:
        events.replace([tmp_path / 'input'])
        monkeypatch.setattr(events, '_readable', lambda timeout: {events._fd})
        calls = []

        def read(*args):
            calls.append(True)
            assert len(calls) <= 16, 'continuous writer starved caller'
            return struct.pack('iIII', -1, 0x4000, 0, 0)

        monkeypatch.setattr(module.os, 'read', read)
        assert events.wait(0)
        assert not events.control_only


def test_file_write_wakes_without_input_polling(tmp_path):
    source = tmp_path / 'input'
    source.write_text('before')
    with FileEvents() as events:
        events.replace([str(source)])
        assert not events.wait(0)
        source.write_text('after')
        assert events.wait(0.8)
        assert not events.wait(0)


def test_high_descriptors_deliver_source_and_explicit_wake(tmp_path):
    import fcntl
    import resource

    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1100:
        pytest.skip('host cannot hold a descriptor above FD_SETSIZE')
    source = tmp_path / 'high-fd-input'
    with FileEvents() as events:
        events.replace([source])
        # Duplicate real descriptors, without consuming a thousand unrelated FDs.
        for attribute in ('_fd', '_wake_read'):
            old = getattr(events, attribute)
            high = fcntl.fcntl(old, fcntl.F_DUPFD_CLOEXEC, 1100)
            setattr(events, attribute, high)
            os.close(old)
        source.write_text('changed')
        assert events.wait(.8)
        assert not events.control_only
        assert not events.wait(0)
        events.wake()
        assert events.wait(.8)
        assert not events.control_only


def test_wake_after_owner_close_is_harmless():
    events = FileEvents()
    events.close()
    events.wake()
    events.close()


def test_control_notifications_do_not_hide_source_events(tmp_path):
    source, demand = tmp_path / 'input', tmp_path / 'demand'
    with FileEvents() as events:
        events.replace([source], controls=[demand])
        demand.write_text('interest')
        assert events.wait(.8)
        assert events.control_only
        demand.write_text('renewed')
        source.write_text('changed')
        assert events.wait(.8)
        assert not events.control_only
        events.wake()
        assert events.wait(.8)
        assert not events.control_only


def test_replaced_subscription_ignores_removed_source(tmp_path):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.write_text('before')
    new.write_text('before')
    with FileEvents() as events:
        events.replace([str(old)])
        events.replace([str(new)])
        old.write_text('after')
        assert not events.wait(0.1)
        new.unlink()
        assert events.wait(0.8)


def test_missing_ancestors_are_covered_before_snapshot(tmp_path):
    source = tmp_path / 'missing' / 'nested' / 'state.db'
    with FileEvents() as events:
        events.replace([source])
        source.parent.mkdir(parents=True)
        assert events.wait(0.8)
        events.replace([source])
        source.write_text('created')
        assert events.wait(0.8)
        events.replace([source])
        source.write_text('updated')
        assert events.wait(0.8)


def test_failed_install_keeps_previous_subscription_without_fd_leak(tmp_path, monkeypatch):
    old = tmp_path / 'old'
    with FileEvents() as events:
        events.replace([old])
        before = set(os.listdir('/proc/self/fd'))
        add = events._lib.inotify_add_watch
        calls = 0

        def failing_add(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                module.ctypes.set_errno(errno.ENOSPC)
                return -1
            return add(*args)

        monkeypatch.setattr(events._lib, 'inotify_add_watch', failing_add)
        with pytest.raises(OSError, match='No space'):
            events.replace([tmp_path / 'other'])
        assert set(os.listdir('/proc/self/fd')) == before
        old.write_text('still watched')
        assert events.wait(0.8)


def test_explicit_wake_interrupts_wait_without_filesystem_reads(tmp_path):
    import threading
    with FileEvents() as events:
        events.replace([tmp_path / 'input'])
        result = []
        thread = threading.Thread(target=lambda: result.append(events.wait(5)))
        thread.start()
        try:
            events.wake()
            thread.join(0.8)
            assert not thread.is_alive()
            assert result == [True]
            assert not events.wait(0)
        finally:
            thread.join(6)


def test_directory_discovery_and_wal_creation(tmp_path):
    with FileEvents() as events:
        events.replace([tmp_path / 'state.db'], directories=[tmp_path / 'boards'])
        (tmp_path / 'state.db-wal').write_text('wal')
        assert events.wait(0.8)
        (tmp_path / 'boards').mkdir()
        assert events.wait(0.8)
        events.replace([tmp_path / 'state.db'], directories=[tmp_path / 'boards'])
        (tmp_path / 'boards' / 'new-board').mkdir()
        assert events.wait(0.8)


def test_parent_replacement_rearms(tmp_path):
    parent = tmp_path / 'parent'
    parent.mkdir()
    source = parent / 'input'
    with FileEvents() as events:
        events.replace([source])
        parent.rename(tmp_path / 'retired')
        parent.mkdir()
        assert events.wait(0.8)
        events.replace([source])
        source.write_text('new inode')
        assert events.wait(0.8)


def test_symlink_target_write_and_retarget_are_observed(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    with FileEvents() as events:
        events.replace([alias / 'input'])
        (target / 'input').write_text('first')
        assert events.wait(0.8)
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        assert events.wait(0.8)
        events.replace([alias / 'input'])
        (other / 'input').write_text('second')
        assert events.wait(0.8)


def test_file_symlink_target_and_missing_target_creation(tmp_path):
    target = tmp_path / 'outside' / 'nested' / 'input'
    alias = tmp_path / 'input-link'
    alias.symlink_to(target)
    with FileEvents() as events:
        events.replace([alias])
        target.parent.mkdir(parents=True)
        assert events.wait(0.5), 'creation of missing symlink target ancestors was missed'
        events.replace([alias])
        target.write_text('created')
        assert events.wait(0.5)
        events.replace([alias])
        target.write_text('changed')
        assert events.wait(0.5), 'direct writes to a file symlink target were missed'


def test_read_error_propagates_and_can_reinstall(tmp_path, monkeypatch):
    source = tmp_path / 'input'
    with FileEvents() as events:
        events.replace([source])
        source.write_text('before')
        with monkeypatch.context() as patch:
            def failed_read(*args):
                raise OSError(errno.EIO, 'injected read failure')
            patch.setattr(module.os, 'read', failed_read)
            with pytest.raises(OSError, match='injected read failure'):
                events.wait(0.8)
        events.replace([source])
        source.write_text('after')
        assert events.wait(0.8)


def test_unsupported_platform_has_no_polling_fallback(monkeypatch):
    monkeypatch.setattr(module.sys, 'platform', 'unsupported')
    with pytest.raises(NotImplementedError):
        FileEvents()

