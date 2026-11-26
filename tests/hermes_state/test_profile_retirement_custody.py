"""Named profile deletion must wait for SQLite owners in every process."""
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from hermes_constants import clear_named_profile_deleted, mark_named_profile_deleted
from hermes_state import SessionDB
from tests.waiting import wait_until


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    root.mkdir()
    (root / 'config.yaml').write_text('{}')
    home = root / 'profiles' / 'example'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}')
    monkeypatch.setenv('HERMES_HOME', str(root))
    return home


@pytest.mark.parametrize('kind', ['bare', 'shared', 'readonly'])
def test_remote_owner_retires_without_process_restart(profile_home, kind):
    from hermes_profile_resources import wait_profile_resources
    with SessionDB(profile_home / 'state.db'):
        pass
    code = '''
import sys
from pathlib import Path
from hermes_state import SessionDB
from hermes_state_registry import acquire
home, kind = Path(sys.argv[1]), sys.argv[2]
db = acquire(home / 'state.db') if kind == 'shared' else SessionDB(home / 'state.db', read_only=kind == 'readonly')
print('ready', flush=True)
sys.stdin.readline()
print(db._conn is None, flush=True)
sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(profile_home), kind],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ), cwd=Path(__file__).resolve().parents[2])
    try:
        assert child.stdout.readline().strip() == 'ready'
        mark_named_profile_deleted(profile_home)
        assert wait_profile_resources(profile_home, timeout=10)
        child.stdin.write('check\n')
        child.stdin.flush()
        assert child.stdout.readline().strip() == 'True'
        assert child.poll() is None
        # Same pathname, new generation, while the original process is still alive.
        import shutil
        shutil.rmtree(profile_home)
        profile_home.mkdir()
        (profile_home / 'config.yaml').write_text('{}')
        clear_named_profile_deleted(profile_home)
        with SessionDB(profile_home / 'state.db') as replacement:
            replacement.create_session('new', source='cli', model='test')
            assert replacement.get_session('new') is not None
    finally:
        child.communicate('\n', timeout=10)
        assert child.returncode == 0


def test_checked_out_reader_keeps_removal_barrier(profile_home):
    from hermes_profile_resources import wait_profile_resources
    db = SessionDB(profile_home / 'state.db')
    try:
        with db._read_ctx() as conn:
            assert conn is not db._conn  # a real independently owned pooled reader
            mark_named_profile_deleted(profile_home)
            wait_until(lambda: db._conn is None, timeout=10)
            assert not wait_profile_resources(profile_home, timeout=0)
            assert conn.execute('SELECT 1').fetchone()[0] == 1
        assert wait_profile_resources(profile_home, timeout=10)
    finally:
        db.close()


def test_constructor_is_owned_before_sqlite_open(profile_home, monkeypatch):
    from hermes_profile_resources import wait_profile_resources
    entered, proceed = threading.Event(), threading.Event()
    original = SessionDB._open_writer
    result = []

    def held_open(self):
        entered.set()
        assert proceed.wait(10)
        return original(self)

    monkeypatch.setattr(SessionDB, '_open_writer', held_open)
    def construct():
        try:
            result.append(SessionDB(profile_home / 'state.db'))
        except FileNotFoundError:
            result.append('refused')
    thread = threading.Thread(target=construct)
    thread.start()
    try:
        assert entered.wait(10)
        mark_named_profile_deleted(profile_home)
        assert not wait_profile_resources(profile_home, timeout=0)
    finally:
        proceed.set()
        thread.join(10)
    assert not thread.is_alive()
    assert result == ['refused']
    assert wait_profile_resources(profile_home, timeout=10)


def test_deleted_owner_never_reopens_into_same_name(profile_home):
    from hermes_profile_resources import wait_profile_resources
    db = SessionDB(profile_home / 'state.db')
    mark_named_profile_deleted(profile_home)
    assert wait_profile_resources(profile_home, timeout=10)
    clear_named_profile_deleted(profile_home)
    # Even an old handle with the same directory still present is retired permanently.
    with pytest.raises((FileNotFoundError, RuntimeError)):
        db.create_session('stale', source='cli', model='test')


def test_other_profile_unaffected(profile_home):
    from hermes_profile_resources import wait_profile_resources
    other = profile_home.parent / 'other'
    other.mkdir()
    (other / 'config.yaml').write_text('{}')
    with SessionDB(other / 'state.db') as bystander:
        with SessionDB(profile_home / 'state.db') as db:
            mark_named_profile_deleted(profile_home)
            assert wait_profile_resources(profile_home, timeout=10)
            assert db._conn is None
            assert bystander._conn is not None
            bystander.create_session('unaffected', source='cli', model='test')
