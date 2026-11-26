"""Closed connection owners must not accumulate behind another owner's raw probes."""
import os

from hermes_constants import mark_named_profile_deleted
from hermes_profile_resources import wait_profile_resources
from hermes_state import SessionDB
from hermes_state_dbfile import _pread_db_range, header_probes_under
from hermes_cli.sqlite_safe_read import pread_lock_safe
from tests.hermes_state import test_profile_retirement_custody as helpers

profile_home = helpers.profile_home


def test_transient_connection_leases_drain_while_keeper_pins_probes(profile_home):
    keeper = SessionDB(profile_home / 'state.db')
    transients = []
    try:
        path = profile_home / 'state.db'
        assert _pread_db_range(path, 0, 16) == b'SQLite format 3\x00'
        assert pread_lock_safe(path, 0, 16) == b'SQLite format 3\x00'
        probe_fds = [r[0] for r in header_probes_under(profile_home).values()]
        assert probe_fds
        for _ in range(8):
            db = SessionDB(path, read_only=True)
            lease = db._profile_lease
            assert lease is not None
            db.close()
            lease.finish_if_drained()
            transients.append(lease)
        assert all(lease.handle.closed for lease in transients)
        assert all(not lease.path.exists() for lease in transients)
        assert not keeper._profile_lease.handle.closed
        for fd in probe_fds:
            os.fstat(fd)  # no unsafe close of another owner's probe
        mark_named_profile_deleted(profile_home)
        assert wait_profile_resources(profile_home, timeout=10)
        for fd in probe_fds:
            try:
                os.fstat(fd)
            except OSError:
                continue
            raise AssertionError('probe outlived deletion acknowledgement')
    finally:
        keeper.close()
        for lease in transients:
            lease.finish_if_drained()
