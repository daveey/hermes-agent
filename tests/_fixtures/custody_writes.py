"""Exercise the custody inventory at SQLite's runtime write boundary. [daveey]"""
import sqlite3

import pytest

from tests.hermes_cli.custody_authorizer import CustodyGuard


@pytest.fixture(autouse=True, scope="session")
def custody_writes_are_audited():
    # Module/session templates create real triggers before function fixtures run. Keep their
    # observed definition custody for the whole process, including later copied test homes.
    guard = CustodyGuard()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", guard.factory(sqlite3.connect))
        yield guard
    assert not guard.violations, f"unaudited runtime custody writers: {guard.violations}"
