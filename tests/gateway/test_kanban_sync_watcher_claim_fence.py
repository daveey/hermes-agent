"""The gateway's sync watcher fences this process's dispatcher claims from startup, before it holds .sync.lock.

PR #2715 (t_01a1d6c4e700958e): a fresh host claims nothing on a primary board until its sync loop's first round
reaches the feed head. The watcher declares that intent (``kanban_sync_claims.loop_expected``) before it first tries
the host sync lock, so a dispatcher tick in the interval while it waits for the lock cannot slip a claim through.
While it only waits, it is not the lock owner: it publishes no record of its own and follows the current owner's
record, so the owner's completed round opens claims here too.

Audit t_01a1d839f0aa6622 found ``loop_expected()`` -> ``pass`` survived every existing test; these drive the real
watcher and a real contended ``.sync.lock`` and pause it in its lock-retry wait.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import asyncio
import json
import os

import pytest

from gateway import kanban_watchers as watchers
from gateway.kanban_watchers_common import _acquire_singleton_lock, _release_singleton_lock
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_backlog as backlog
from hermes_cli import kanban_sync_claims as claims
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "worker-a1", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    backlog.reset()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    home = support.kanban_home(tmp_path, monkeypatch)
    yield home
    backlog.reset()
    sync.set_lease_client(None)


def _board(kanban_home, *, enabled):
    support.write_routed_sync_config(kanban_home, True)
    svc = FakeService(lease_runs=True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    support.write_routed_sync_config(kanban_home, enabled)  # the switch the watcher reads at startup
    return conn


def _tick(conn):
    return kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: os.getpid(), board=BOARD)


def _runner(monkeypatch):
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True

    def no_loop(*_a, **_k):
        raise AssertionError("the sync loop must not start while another process holds .sync.lock")
    monkeypatch.setattr(ksc, "run_forever", no_loop)
    return runner


def test_waiting_sync_watcher_fences_claims_until_the_lock_owners_round_opens_them(kanban_home, monkeypatch):
    conn = _board(kanban_home, enabled=True)
    tid = kb.create_task(conn, title="canary", assignee="default")
    key = os.path.realpath(kanban_home / "kanban.db")
    lock = kb.kanban_home() / "kanban" / ".sync.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    owner, state = _acquire_singleton_lock(lock)  # another gateway on this host runs the sync loop
    assert state == "held"
    runner = _runner(monkeypatch)
    seen: list = []

    async def paused(_interval):
        """The watcher's lock-retry wait: it lost try_acquire and holds no sync tenure."""
        lease = runner._kanban_sync_lease
        assert lease.state != "held" and not claims._STATE.loop
        if not seen:
            # Startup interval: the owner has not published a record yet. Fail closed; the waiter writes none.
            assert not claims.state_path().exists()
            result = _tick(conn)
            seen.append((result.claim_deferred, kb.get_task(conn, tid).status))
            # The owner's loop starts its tenure and its first round reaches the head.
            claims.state_path().write_text(json.dumps({"pid": os.getppid(), "open": {key: BOARD}}))
            return
        # Still waiting: claims follow the owner's record, not this process's own (empty) readiness.
        _tick(conn)
        seen.append((kb.get_task(conn, tid).status, json.loads(claims.state_path().read_text())["pid"]))
        runner._running = False
    monkeypatch.setattr(runner, "_sleep_between_ticks", paused)
    try:
        asyncio.run(runner._kanban_sync_watcher())
    finally:
        _release_singleton_lock(owner)
        conn.close()
    assert seen == [([(tid, claims.DEFERRAL)], "ready"), ("running", os.getppid())]


def test_failed_lease_acquire_clears_the_claim_hold(kanban_home, monkeypatch):
    """A lease the watcher will not retry must not leave the claim hold set."""
    conn = _board(kanban_home, enabled=True)
    claims.loop_abandoned()
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True

    async def no_lease(_lease):
        return False

    monkeypatch.setattr(runner, "_acquire_kanban_lease", no_lease)
    try:
        asyncio.run(runner._kanban_sync_watcher())
        assert not claims._STATE.expected
    finally:
        conn.close()


def test_unyielded_sync_return_clears_the_claim_hold(kanban_home, monkeypatch):
    """A sync loop that ends without yielding the lease will not be respawned, so the hold clears."""
    conn = _board(kanban_home, enabled=True)
    claims.loop_abandoned()
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True

    class Lease:
        def should_yield(self):
            return False

        def release(self):
            return None

    monkeypatch.setattr(runner, "_kanban_singleton_lease", lambda *_a, **_k: Lease())

    async def hold(_lease):
        return True

    monkeypatch.setattr(runner, "_acquire_kanban_lease", hold)
    monkeypatch.setattr(ksc, "run_forever", lambda *_a, **_k: None)
    try:
        asyncio.run(runner._kanban_sync_watcher())
        assert not claims._STATE.expected
    finally:
        conn.close()


def test_sync_disabled_watcher_leaves_claims_open(kanban_home, monkeypatch):
    """Control: with sync off the watcher returns at once and declares nothing, so the dispatcher claims as before."""
    conn = _board(kanban_home, enabled=False)
    tid = kb.create_task(conn, title="no sync", assignee="default")
    runner = _runner(monkeypatch)
    try:
        asyncio.run(runner._kanban_sync_watcher())
        assert not claims._STATE.expected and not claims.held(conn)
        _tick(conn)
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()
