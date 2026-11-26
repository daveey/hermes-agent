"""Lease garbage collection must preserve the pre-SQLite publication barrier."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.hermes_state import test_profile_retirement_custody as helpers


def test_exit_cleanup_does_not_acknowledge_unclosed_sqlite(profile_home):
    from hermes_constants import mark_named_profile_deleted
    from hermes_profile_resources import _cleanup_drained_at_exit, _resources_unowned, wait_profile_resources
    db = helpers.SessionDB(profile_home / 'state.db')
    try:
        _cleanup_drained_at_exit()
        assert db._profile_lease.path.exists()
        assert db._conn.execute('SELECT 1').fetchone()[0] == 1
        assert not _resources_unowned(profile_home)
    finally:
        db.close()
    mark_named_profile_deleted(profile_home)
    assert wait_profile_resources(profile_home, timeout=10)

profile_home = helpers.profile_home


def _check_unlink_permissions(tmp_path, monkeypatch, foreign_open):
    import hermes_profile_resources as resources
    path = tmp_path / 'lease'
    handle = path.open('x+')
    original = Path.unlink

    def guarded_unlink(self, **kwargs):
        if not handle.closed or foreign_open:
            raise PermissionError('open file')
        return original(self, **kwargs)

    monkeypatch.setattr(Path, 'unlink', guarded_unlink)
    try:
        if os.name != 'nt':
            with pytest.raises(PermissionError):
                resources._unlink_lease(path, handle)
            assert not handle.closed
        else:
            resources._unlink_lease(path, handle)
            assert handle.closed
            assert path.exists() == foreign_open
    finally:
        handle.close()


@pytest.mark.platforms('posix')
def test_unlink_posix_permissions_propagate(tmp_path, monkeypatch):
    _check_unlink_permissions(tmp_path, monkeypatch, False)


@pytest.mark.platforms('windows')
@pytest.mark.parametrize('foreign_open', [False, True])
def test_unlink_windows_open_file_fallback(tmp_path, monkeypatch, foreign_open):
    _check_unlink_permissions(tmp_path, monkeypatch, foreign_open)


@pytest.mark.platforms('posix')
def test_unlink_preserves_replacement_path(tmp_path):
    from hermes_profile_resources import _unlink_lease
    path = tmp_path / 'lease'
    with path.open('x+') as handle:
        path.unlink()
        path.write_text('replacement')
        _unlink_lease(path, handle)
        assert path.read_text() == 'replacement'


def _child(home, body):
    code = '''
from pathlib import Path
import os
import sys
from hermes_state import SessionDB
from hermes_profile_resources import ProfileResourceLease
home = Path(sys.argv[1])
''' + body
    result = subprocess.run([sys.executable, '-c', code, str(home)],
                            cwd=Path(__file__).resolve().parents[2],
                            env=dict(os.environ), capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr


def test_clean_close_exit_removes_lease_files(profile_home):
    for _ in range(3):
        _child(profile_home, "db = SessionDB(home / 'state.db')\ndb.close()\n")
        directory = profile_home.parent / '.resource-leases' / profile_home.name
        assert list(directory.iterdir()) == []


def test_next_acquisition_reclaims_crash_residue_but_keeps_live_owner(profile_home):
    from hermes_profile_resources import ProfileResourceLease
    live = ProfileResourceLease(profile_home)
    try:
        for _ in range(3):
            _child(profile_home, 'lease = ProfileResourceLease(home)\nos._exit(0)\n')
        replacement = ProfileResourceLease(profile_home)
        try:
            assert live.path.exists()
            assert set(live.path.parent.iterdir()) == {live.path, replacement.path}
        finally:
            replacement.release_owner()
            replacement.finish_if_drained()
    finally:
        live.release_owner()
        live.finish_if_drained()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('replacement', [False, True])
def test_opener_retries_if_sweep_unlinks_before_lock(profile_home, monkeypatch, replacement):
    from gateway import status
    from hermes_profile_resources import ProfileResourceLease, _resources_unowned
    original = status._try_acquire_file_lock
    swept = []

    def interleaved(handle):
        path = Path(handle.name)
        if not swept:
            swept.append(path)
            assert _resources_unowned(profile_home)
            assert not path.exists()
            if replacement:
                path.write_text('replacement')
        return original(handle)

    monkeypatch.setattr(status, '_try_acquire_file_lock', interleaved)
    lease = ProfileResourceLease(profile_home)
    try:
        assert lease.path != swept[0]
        assert lease.path.exists()
        if replacement:
            assert swept[0].read_text() == 'replacement'
        assert not _resources_unowned(profile_home)
    finally:
        lease.release_owner()
        lease.finish_if_drained()
