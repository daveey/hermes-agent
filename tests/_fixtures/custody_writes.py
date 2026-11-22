"""Exercise the custody inventory at SQLite's runtime write boundary. [daveey]"""
import sqlite3

import pytest

from tests.hermes_cli.custody_authorizer import CustodyGuard


@pytest.fixture(autouse=True)
def custody_writes_are_audited(monkeypatch):
    guard = CustodyGuard()
    monkeypatch.setattr(sqlite3, "connect", guard.factory(sqlite3.connect))
    yield guard
    assert not guard.violations, f"unaudited runtime custody writers: {guard.violations}"
