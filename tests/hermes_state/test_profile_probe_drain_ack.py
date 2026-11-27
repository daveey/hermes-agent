"""Probe-owner acknowledgement follows actual lock-safe raw descriptor closure."""
import os
import sqlite3

import pytest

import hermes_profile_resources as resources
import hermes_state_dbfile as dbfile
from hermes_cli import sqlite_safe_read as reader
from tests.hermes_state import test_profile_retirement_custody as helpers
from tests.waiting import wait_until

profile_home = helpers.profile_home


@pytest.mark.skipif(os.name == 'nt', reason='POSIX cached raw probes and descriptor custody')
@pytest.mark.parametrize('kind', ['header', 'pread'])
def test_probe_owner_cannot_acknowledge_while_raw_probe_is_pinned(profile_home, kind):
    path = profile_home / 'state.db'
    # A real SQLite connection pins the inode without opening a second kind
    # of diagnostic probe; otherwise the header gate masks a missing pread gate.
    keeper = sqlite3.connect(path)
    keeper.execute('CREATE TABLE sentinel(value INTEGER)')
    keeper.commit()
    lease = None
    try:
        if kind == 'header':
            assert dbfile._pread_db_range(path, 0, 16) == b'SQLite format 3\x00'
            fd = dbfile.header_probes_under(profile_home)[str(path)][0]
            assert str(path) not in reader._PREAD_FDS
        else:
            assert reader.pread_lock_safe(path, 0, 16) == b'SQLite format 3\x00'
            fd = reader._PREAD_FDS[str(path)][0]
            assert dbfile.all_header_probes_under(profile_home) == []
        owner = resources._probe_owners[profile_home]
        lease = owner.lease
        resources._retire_probe_owner(owner)
        # The live connection pins the real inode. Each independent drain gate
        # must retain both the raw descriptor and its published lease, even when
        # the probe owner itself has released its reference.
        lease.finish_if_drained()
        assert os.pread(fd, 16, 0) == b'SQLite format 3\x00'
        assert not lease.handle.closed
        assert lease.path.exists()
        assert lease in resources._leases
        keeper.close()
        wait_until(lambda: lease.handle.closed, timeout=10)
        assert not lease.path.exists()
        assert lease not in resources._leases
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        keeper.close()
        if lease is not None:
            lease.finish_if_drained()
