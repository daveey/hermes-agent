"""A stalled full scan must not own the schema turn of unrelated real CLI creates."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading

import pytest

from tests.hermes_cli import test_enrollment_cli_create as cli
from tests.hermes_cli.test_enrollment_cli_create import await_marker, create_argv

sandbox = cli.sandbox

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("kind", ["canary", "failure-report"])
def test_stalled_scan_does_not_block_create_with_live_wal_writer(sandbox, kind):
    box = sandbox
    # This independent fixture table keeps the DB changing; no health-cache
    # priming, forged stamps or relaxed freshness predicates can pass the test.
    with sqlite3.connect(box.db) as conn:
        conn.execute("CREATE TABLE fixture_writer (value INTEGER)")
        conn.execute("INSERT INTO fixture_writer VALUES (0)")
    stop = threading.Event()
    committed = threading.Event()
    failures = []

    def writer():
        try:
            with sqlite3.connect(box.db, timeout=5) as conn:
                while not stop.is_set():
                    conn.execute("UPDATE fixture_writer SET value=value+1")
                    conn.commit()
                    committed.set()
                    # health: allow HX013 -- bounded background writer models concurrent WAL changes, not readiness
                    stop.wait(.02)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert committed.wait(5)
        blocked = box.start("integrity-gate", "blocked", create_argv("canary", "convoy:blocked"))
        await_marker(box, "blocked", ".integrity-entered", blocked)
        sibling = box.start("normal", "sibling", create_argv(kind, "convoy:sibling"))
        try:
            out, err = sibling.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            box.stop(sibling)
            pytest.fail("unrelated actual CLI create queued behind a stalled integrity scan")
        assert sibling.returncode == 0, (out, err)
        assert (box.root / "sibling.integrity-done").exists(), "must retain its own full integrity scan"
        assert blocked.poll() is None
        assert len(box.rows()) == 1
        assert box.rows()[0]["id"] == json.loads(out)["id"]
        with sqlite3.connect(box.db) as conn:
            assert conn.execute("SELECT value FROM fixture_writer").fetchone()[0] > 1
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert failures == []
