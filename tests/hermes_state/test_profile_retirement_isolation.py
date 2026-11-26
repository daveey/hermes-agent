"""Retirement preserves live-profile reopen and isolation from slow foreign owners."""
import threading

from hermes_constants import get_hermes_home, mark_named_profile_deleted
from hermes_profile_resources import ProfileResourceLease, wait_profile_resources
from hermes_state import SessionDB
from tests.hermes_state import test_profile_retirement_custody as helpers

profile_home = helpers.profile_home


def test_live_profile_can_reopen_with_fresh_custody(profile_home):
    db = SessionDB(profile_home / 'state.db')
    original = db._profile_lease
    try:
        db.create_session('before', source='cli', model='test')
        db.close()
        db.create_session('after', source='cli', model='test')
        assert db._profile_lease is not original
        assert db.get_session('after') is not None
        mark_named_profile_deleted(profile_home)
        assert wait_profile_resources(profile_home, timeout=10)
        assert db._conn is None
    finally:
        db.close()


def test_slow_foreign_owner_does_not_block_retirement(profile_home):
    other = profile_home.parent / 'other'
    other.mkdir()
    (other / 'config.yaml').write_text('{}')
    slow, fast = ProfileResourceLease(profile_home), ProfileResourceLease(other)
    entered, proceed = threading.Event(), threading.Event()
    observed = []
    class Owner:
        pass
    slow_owner, fast_owner = Owner(), Owner()
    def close_slow(owner):
        entered.set()
        assert proceed.wait(10)
        slow.release_owner()
    def close_fast(owner):
        observed.append(get_hermes_home())
        fast.release_owner()
    slow.set_owner(slow_owner, retire=close_slow)
    fast.set_owner(fast_owner, retire=close_fast)
    try:
        mark_named_profile_deleted(profile_home)
        assert entered.wait(10)
        mark_named_profile_deleted(other)
        assert wait_profile_resources(other, timeout=3)
        assert observed == [other]  # callbacks run in the retired owner's profile scope
        assert not wait_profile_resources(profile_home, timeout=0)
    finally:
        proceed.set()
        assert wait_profile_resources(profile_home, timeout=10)
        assert wait_profile_resources(other, timeout=10)


def test_probe_drain_does_not_wait_for_foreign_pread(profile_home):
    from hermes_cli import sqlite_safe_read as reader
    entered, proceed = threading.Event(), threading.Event()
    def read_large_file():
        with reader._PREAD_LOCK:
            entered.set()
            proceed.wait(5)
    thread = threading.Thread(target=read_large_file)
    thread.start()
    try:
        assert entered.wait(5)
        assert reader.release_profile_pread_fds(profile_home) is False
    finally:
        proceed.set()
        thread.join(10)
    assert not thread.is_alive()
    assert reader.release_profile_pread_fds(profile_home) is True
