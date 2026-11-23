"""Account custody barrier is a permanent nonblocking flock, never auto-created at runtime."""
from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import hermes_constants
import hermes_state_guard
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli.kanban_source_barrier import (
    CustodyBusy,
    CustodyUnavailable,
    establish,
    hold,
    lock_path,
)
from tests.waiting import wait_until
from tools.environments.local import build_subprocess_env

pytestmark = pytest.mark.platforms('posix')

REPO = Path(__file__).resolve().parents[2]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _child_env(**extra: str) -> dict[str, str]:
    merged = {
        'PYTHONPATH': os.pathsep.join([str(REPO), os.environ.get('PYTHONPATH', '')]),
    }
    merged.update(extra)
    return build_subprocess_env(extra=merged)


def _release_child(child: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        if child.stdin is not None:
            try:
                child.stdin.write('x')
            except (BrokenPipeError, OSError):
                pass
            try:
                child.stdin.close()
            except OSError:
                pass
    except OSError:
        pass
    return child.communicate(timeout=10)


class _ForeignUid:
    def __init__(self, st: os.stat_result, uid: int) -> None:
        self._st = st
        self.st_uid = uid

    def __getattr__(self, name: str):
        return getattr(self._st, name)


def test_existing_lock_fd_is_nonblocking_and_close_on_exec():
    import fcntl
    establish()
    establish()  # exercise both explicit adoption and runtime existing-file open
    with hold() as held:
        assert fcntl.fcntl(held.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK
        assert fcntl.fcntl(held.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC


def test_directory_and_exclusive_create_descriptors_have_k1_flags(tmp_path):
    import fcntl
    from hermes_cli import kanban_source_barrier as barrier
    directory = barrier.open_directory(tmp_path)
    try:
        assert fcntl.fcntl(directory, fcntl.F_GETFL) & os.O_NONBLOCK
        assert fcntl.fcntl(directory, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        fd = barrier.open_regular('new', os.O_RDWR | os.O_CREAT | os.O_EXCL,
                                  dir_fd=directory, mode=0o600)
        try:
            assert stat.S_ISREG(os.fstat(fd).st_mode)
            assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
            assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
            assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
            os.write(fd, b'kept')
        finally:
            os.close(fd)
        with pytest.raises(FileExistsError):
            barrier.open_regular('new', os.O_RDWR | os.O_CREAT | os.O_EXCL,
                                 dir_fd=directory, mode=0o600)
        assert (tmp_path / 'new').read_bytes() == b'kept'
    finally:
        os.close(directory)


def test_lock_lives_beside_future_pins_under_isolated_policy_root():
    path = lock_path()
    assert path == capacity.policy_root() / 'source-custody' / 'pins.lock'
    assert path.is_absolute()
    assert not path.exists()
    assert not path.with_name('pins.sqlite3').exists()


def test_runtime_hold_does_not_create_a_missing_lock():
    path = lock_path()
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('missing lock must not be acquired')
    assert not path.exists()
    assert not path.parent.exists()


def test_establish_creates_private_owned_nonsymlink_dir_and_lock():
    path = establish()
    assert path == lock_path()
    directory = path.parent
    dir_st = directory.lstat()
    file_st = path.lstat()
    assert not stat.S_ISLNK(dir_st.st_mode) and stat.S_ISDIR(dir_st.st_mode)
    assert not stat.S_ISLNK(file_st.st_mode) and stat.S_ISREG(file_st.st_mode)
    assert dir_st.st_uid == os.geteuid() and file_st.st_uid == os.geteuid()
    assert _mode(directory) == 0o700
    assert _mode(path) == 0o600
    assert not path.with_name('pins.sqlite3').exists()
    assert not (capacity.policy_root() / 'source-custody.activation.json').exists()


def test_establish_is_idempotent_and_does_not_replace_inode():
    first = establish()
    identity = first.lstat()
    second = establish()
    again = second.lstat()
    assert (identity.st_dev, identity.st_ino) == (again.st_dev, again.st_ino)


def test_establish_refuses_after_activation_marker():
    establish()
    marker = capacity.policy_root() / 'source-custody.activation.json'
    marker.write_text('{}', encoding='utf-8')
    with pytest.raises(CustodyUnavailable):
        establish()
    assert lock_path().is_file()


def test_hold_and_release_keep_the_lock_file():
    path = establish()
    before = path.lstat()
    with hold() as held:
        assert not os.get_inheritable(held.fileno())
    after = path.lstat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert path.is_file()


def test_independent_opens_in_one_thread_are_busy():
    establish()
    with hold():
        with pytest.raises(CustodyBusy):
            with hold():
                raise AssertionError('nested independent open must not share the lock')


def test_thread_contention_is_busy_without_sleep():
    establish()
    acquired = threading.Event()
    finished = threading.Event()
    result = []

    def contender():
        wait_until(acquired.is_set, timeout=5, what='holder acquired')
        try:
            with hold():
                result.append('held')
        except CustodyBusy:
            result.append('busy')
        finally:
            finished.set()

    worker = threading.Thread(target=contender, daemon=True)
    worker.start()
    with hold():
        acquired.set()
        wait_until(finished.is_set, timeout=5, what='contender refused')
    worker.join(timeout=5)
    assert result == ['busy']


def test_process_contention_is_busy():
    establish()
    env = _child_env(HERMES_TEST_POLICY_ROOT=str(capacity.policy_root()))
    child = subprocess.Popen(
        [sys.executable, '-c',
         'from hermes_cli.kanban_source_barrier import hold\n'
         'import sys\n'
         'held = hold()\n'
         'held.__enter__()\n'
         'sys.stdout.write("held\\n")\n'
         'sys.stdout.flush()\n'
         'sys.stdin.read(1)\n'
         'held.__exit__(None, None, None)\n'],
        cwd=str(REPO), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8',
    )
    try:
        assert child.stdout is not None
        os.set_blocking(child.stdout.fileno(), False)

        def held_line():
            if child.poll() is not None:
                rest = child.stdout.read() if child.stdout is not None else ''
                return rest or 'exited'
            try:
                line = child.stdout.readline()
            except BlockingIOError:
                return False
            return line or False

        line = wait_until(held_line, timeout=10, what='child held barrier')
        assert line.strip() == 'held'
        with pytest.raises(CustodyBusy):
            with hold():
                raise AssertionError('parent must not acquire while the child holds')
    finally:
        stdout, stderr = _release_child(child)
        assert child.returncode == 0, stdout + stderr


def test_exception_releases_without_unlinking():
    path = establish()
    identity = path.lstat()
    with pytest.raises(RuntimeError, match='boom'):
        with hold():
            raise RuntimeError('boom')
    after = path.lstat()
    assert (identity.st_dev, identity.st_ino) == (after.st_dev, after.st_ino)
    with hold():
        pass


def test_exec_cannot_inherit_the_lock_descriptor():
    establish()
    with hold() as held:
        fd = held.fileno()
        assert not os.get_inheritable(fd)
        probe = ('import os, sys\nfd = int(sys.argv[1])\n'
                 'try:\n    os.fstat(fd)\nexcept OSError:\n    print("closed")\n'
                 'else:\n    print("INHERITED")\n')
        out = subprocess.run(
            [sys.executable, '-c', probe, str(fd)], close_fds=False,
            capture_output=True, text=True, encoding='utf-8', timeout=10, check=True,
            env=_child_env(),
        ).stdout
    assert out.strip() == 'closed'


def test_unreadable_lock_is_unavailable():
    if os.geteuid() == 0:
        pytest.skip('root bypasses file mode')
    path = establish()
    os.chmod(path, 0)
    try:
        with pytest.raises(CustodyUnavailable):
            with hold():
                raise AssertionError('unreadable lock must fail closed')
    finally:
        os.chmod(path, 0o600)


def test_unreadable_directory_is_unavailable():
    if os.geteuid() == 0:
        pytest.skip('root bypasses directory mode')
    path = establish()
    os.chmod(path.parent, 0)
    try:
        with pytest.raises(CustodyUnavailable):
            with hold():
                raise AssertionError('unreadable directory must fail closed')
    finally:
        os.chmod(path.parent, 0o700)


def test_wrong_type_lock_is_unavailable():
    establish()
    path = lock_path()
    path.unlink()
    path.mkdir()
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('directory lock must fail closed')


def test_world_readable_lock_is_unavailable():
    path = establish()
    os.chmod(path, 0o644)
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('world-readable lock must fail closed')


def test_symlink_lock_is_unavailable(tmp_path):
    establish()
    path = lock_path()
    target = tmp_path / 'elsewhere.lock'
    target.write_bytes(b'')
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('symlink lock must fail closed')


def test_symlink_directory_is_unavailable(tmp_path):
    establish()
    directory = lock_path().parent
    real = tmp_path / 'moved'
    os.rename(directory, real)
    directory.symlink_to(real)
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('symlink custody directory must fail closed')


def test_replaced_inode_is_unavailable(monkeypatch):
    establish()
    path = lock_path()
    real_open = os.open

    def open_then_replace(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        name = args[0] if args else kwargs.get('path')
        if name == 'pins.lock':
            replacement = path.with_name('pins.lock.new')
            replacement.write_bytes(b'')
            os.chmod(replacement, 0o600)
            os.replace(replacement, path)
        return fd

    monkeypatch.setattr(os, 'open', open_then_replace)
    with pytest.raises(CustodyUnavailable, match='inode was replaced'):
        with hold():
            raise AssertionError('replaced lock inode must fail closed')


def test_stat_failure_before_flock_is_unavailable_and_releases(monkeypatch):
    establish()
    real_stat = os.stat
    pins = {'n': 0}

    def fail_on_revalidate(*args, **kwargs):
        name = args[0] if args else kwargs.get('path')
        if name == 'pins.lock':
            pins['n'] += 1
            if pins['n'] >= 2:
                raise OSError(errno.EIO, 'injected stat failure')
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(os, 'stat', fail_on_revalidate)
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('pre-flock stat failure must not escape untyped')
    monkeypatch.setattr(os, 'stat', real_stat)
    with hold():
        pass


def test_stat_failure_after_flock_is_unavailable_and_releases(monkeypatch):
    establish()
    import fcntl
    real_flock = fcntl.flock
    real_stat = os.stat
    armed = {'on': False}

    def flock_and_arm(fd, op):
        real_flock(fd, op)
        if op & fcntl.LOCK_EX:
            armed['on'] = True

    def fail_after_flock(*args, **kwargs):
        name = args[0] if args else kwargs.get('path')
        if armed['on'] and name == 'pins.lock':
            raise OSError(errno.EIO, 'injected stat failure')
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(fcntl, 'flock', flock_and_arm)
    monkeypatch.setattr(os, 'stat', fail_after_flock)
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('post-flock stat failure must not escape untyped')
    monkeypatch.setattr(os, 'stat', real_stat)
    monkeypatch.setattr(fcntl, 'flock', real_flock)
    with hold():
        pass


@pytest.mark.parametrize('exc_type', [KeyboardInterrupt, SystemExit])
def test_open_cleanup_preserves_keyboardinterrupt_and_systemexit(monkeypatch, exc_type):
    establish()
    real_open = os.open

    def interrupt(*args, **kwargs):
        name = args[0] if args else kwargs.get('path')
        if name == 'pins.lock':
            raise exc_type()
        return real_open(*args, **kwargs)

    monkeypatch.setattr(os, 'open', interrupt)
    with pytest.raises(exc_type):
        with hold():
            raise AssertionError('must not convert interrupt into unavailable')


def test_fstat_keyboard_interrupt_after_open_is_preserved(monkeypatch):
    establish()
    real_fstat = os.fstat

    def interrupt(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode):
            raise KeyboardInterrupt
        return st

    monkeypatch.setattr(os, 'fstat', interrupt)
    with pytest.raises(KeyboardInterrupt):
        with hold():
            raise AssertionError('must not convert interrupt into unavailable')


def test_establish_fsyncs_lock_directory_and_policy_root(monkeypatch):
    seen: list[os.stat_result] = []
    real_fsync = os.fsync

    def record(fd):
        seen.append(os.fstat(fd))
        return real_fsync(fd)

    monkeypatch.setattr(os, 'fsync', record)
    path = establish()
    synced = {(st.st_dev, st.st_ino) for st in seen}
    for target in (path, path.parent, capacity.policy_root()):
        st = target.lstat()
        assert (st.st_dev, st.st_ino) in synced


def test_establish_fsync_failure_is_unavailable(monkeypatch):
    def boom(_fd):
        raise OSError(errno.EIO, 'injected fsync failure')

    monkeypatch.setattr(os, 'fsync', boom)
    with pytest.raises(CustodyUnavailable):
        establish()


def test_establish_rechecks_activation_marker_while_held(monkeypatch):
    import fcntl
    real_flock = fcntl.flock
    marker = capacity.policy_root() / 'source-custody.activation.json'

    def flock_and_mark(fd, op):
        real_flock(fd, op)
        if op & fcntl.LOCK_EX:
            marker.write_text('{}', encoding='utf-8')

    monkeypatch.setattr(fcntl, 'flock', flock_and_mark)
    with pytest.raises(CustodyUnavailable):
        establish()


def test_symlink_policy_root_is_unavailable_on_establish_and_hold(tmp_path, monkeypatch):
    real = tmp_path / 'real-policy'
    real.mkdir()
    monkeypatch.setattr(capacity, 'policy_root', lambda: real)
    establish()
    link = tmp_path / 'link-policy'
    link.symlink_to(real)
    monkeypatch.setattr(capacity, 'policy_root', lambda: link)
    with pytest.raises(CustodyUnavailable):
        establish()
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('must not follow a symlink policy root')


def test_foreign_owned_policy_root_is_unavailable_on_establish_and_hold(monkeypatch):
    establish()
    root = capacity.policy_root()
    identity = root.lstat()
    real_fstat = os.fstat
    real_lstat = os.lstat
    foreign = os.geteuid() + 1

    def is_root(st: os.stat_result) -> bool:
        return (st.st_dev, st.st_ino) == (identity.st_dev, identity.st_ino)

    def fstat(fd):
        st = real_fstat(fd)
        return _ForeignUid(st, foreign) if is_root(st) else st

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        return _ForeignUid(st, foreign) if is_root(st) else st

    monkeypatch.setattr(os, 'fstat', fstat)
    monkeypatch.setattr(os, 'lstat', lstat)
    with pytest.raises(CustodyUnavailable):
        establish()
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('must not use a foreign-owned policy root')


def test_replaced_policy_root_is_not_traversed(tmp_path, monkeypatch):
    establish()
    root = capacity.policy_root()
    foreign = tmp_path / 'foreign-root'
    foreign.mkdir()
    custody = foreign / 'source-custody'
    custody.mkdir(mode=0o700)
    lock = custody / 'pins.lock'
    lock.write_bytes(b'')
    os.chmod(lock, 0o600)
    os.chmod(custody, 0o700)
    real_open = os.open
    swapped = {'done': False}

    def open_after_replace(*args, **kwargs):
        if not swapped['done']:
            moved = tmp_path / 'moved-policy-root'
            os.rename(root, moved)
            root.symlink_to(foreign)
            swapped['done'] = True
        return real_open(*args, **kwargs)

    monkeypatch.setattr(os, 'open', open_after_replace)
    with pytest.raises(CustodyUnavailable):
        with hold():
            raise AssertionError('must not lock through a replaced policy root')


def test_policy_root_replaced_after_lock_open_refuses(tmp_path, monkeypatch):
    import fcntl
    establish()
    root = capacity.policy_root()
    real_flock = fcntl.flock
    moved = tmp_path / 'previous-policy'

    def swap_after_acquisition(fd, op):
        real_flock(fd, op)
        os.rename(root, moved)
        root.mkdir(mode=0o700)

    monkeypatch.setattr(fcntl, 'flock', swap_after_acquisition)
    try:
        with pytest.raises(CustodyUnavailable, match='inode was replaced'):
            with hold():
                raise AssertionError('old root cannot prove current account exclusion')
    finally:
        root.rmdir()
        os.rename(moved, root)


def test_busy_does_not_sleep_or_retry(monkeypatch):
    establish()

    def refuse_sleep(_delay):
        raise AssertionError('barrier must not sleep to retry')

    monkeypatch.setattr('time.sleep', refuse_sleep)
    with hold():
        with pytest.raises(CustodyBusy):
            with hold():
                pass


def test_pytest_establish_refuses_the_real_account_root(monkeypatch, tmp_path):
    monkeypatch.setattr(hermes_constants, '_account_home', lambda: tmp_path)
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path / '.hermes')
    (tmp_path / '.hermes').mkdir()
    with pytest.raises(RuntimeError, match='real account worker inventory'):
        establish()
    assert not (tmp_path / '.hermes' / 'source-custody').exists()
    monkeypatch.delenv('PYTEST_CURRENT_TEST')
    monkeypatch.delenv('PYTEST_VERSION', raising=False)
    monkeypatch.setenv('HERMES_TEST_ISOLATION', str(tmp_path))
    monkeypatch.setattr(hermes_state_guard, '_has_pytest_ancestor', lambda: False)
    with pytest.raises(RuntimeError, match='real account worker inventory'):
        establish()


def test_child_honours_isolated_policy_root_not_the_account(tmp_path):
    root = tmp_path / 'shard' / '.hermes'
    root.mkdir(parents=True)
    env = _child_env(HERMES_TEST_POLICY_ROOT=str(root), PYTHONPATH=str(REPO))
    child = (
        'from hermes_cli.kanban_source_barrier import establish, lock_path\n'
        'from hermes_cli import worker_compute_capacity as capacity\n'
        'path = establish()\n'
        'print(path)\n'
        'print(capacity.policy_root())\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', child], env=env, cwd=str(REPO),
        capture_output=True, text=True, encoding='utf-8', timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    printed = result.stdout.strip().splitlines()
    assert printed[0] == str((root / 'source-custody' / 'pins.lock').resolve())
    assert printed[1] == str(root.resolve())
    assert (root / 'source-custody' / 'pins.lock').is_file()
    assert not (capacity.policy_root() / 'source-custody' / 'pins.lock').exists()
