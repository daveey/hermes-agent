"""A fresh primary live file never replays the whole feed before its outbox moves (t_01a1d65b80d2e9f4).

Incident (autoscaled worker-a1, 2026-11-13): the enroll cold-started the replica from a snapshot (cursor 1920721) but
the primary live file began at cursor 0. Its partitioned pull read 20 pages per round of the board's whole history
(head ~1.93M), so every round ended behind the head, every send ran ``before_head`` and stopped at the oldest pending
op, the claimed run's start row: 24 ops waited 13 min with ``attempts=0``. Among them the requeue's ``status=ready``,
the holder's status-leaving write that ends its Board lease (rows.ts), so the gateway's next claim was deferred
``lease_held holder=worker-a1`` until the Board's TTL expired and charged a failure.

Now a cold live file whose first feed page is not the head rebuilds from a partitioned snapshot (as on a 410), so
the round ends at the head and its send goes out whole. The Board is the in-memory fake (``kanban_sync_fake_board``)
served one change per feed page, so a 60-change backlog is 60 pages.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_neutral_release as neutral
from hermes_cli import kanban_sync_backlog as backlog
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_outbox_age as outbox_age
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_sync_push_starvation import BACKLOG_PAGES, BOARD, ME, _other_creates


class PagedHub:
    """The fake Board's hub for one gateway, serving ONE change per feed page (release ops carry no ``uid``)."""

    def __init__(self, svc, gateway):
        self._inner = svc.hub(gateway)
        self.ident = self._inner.ident

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def changes(self, board, since, limit=500):
        return self._inner.changes(board, since, limit=1)


@pytest.fixture(autouse=True)
def _fresh_loop_state():
    backlog.reset()
    outbox_age.reset()
    yield
    backlog.reset()
    outbox_age.reset()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def live(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    c = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(c, BOARD, ME)
    sync.set_mode(c, "primary")
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield c
    finally:
        sync.set_lease_client(previous)
        c.close()


def _cursor(conn):
    return int((sync.sync_state(conn) or {}).get("cursor") or 0)


def _claimed_on_a_fresh_node(live, kanban_home, svc):
    """The incident's node: a long Board history, a replica cold-started at the head, a live file at cursor 0 and a
    claim whose run start is the oldest pending op."""
    _other_creates(svc, BACKLOG_PAGES)
    tid = kb.create_task(live, title="canary", assignee="worker")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)  # the card reaches the Board; no pull yet
    assert kb.claim_task(live, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    run_id = kb.get_task(live, tid).current_run_id
    uid = sync.run_uid(live, run_id)
    assert tid in svc.board(BOARD)["leases"]
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending' AND table_name = 'task_runs' "
                        "AND row_uid = ?", (uid,)).fetchone()  # the run start, which a before-head send holds
    pulled = ksc._pull_replica(BOARD, svc.hub(ME), ME, kanban_home / "kanban.db", max_pages=None)
    assert not pulled.more  # install-boards' snapshot cold start: the replica is at the head
    assert _cursor(live) == 0
    return tid, run_id, uid


def _round(kanban_home, hub):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", hub, ME)


def test_a_cold_live_file_reaches_the_head_in_one_round_and_sends_its_run_start(live, svc, kanban_home):
    _tid, _run_id, uid = _claimed_on_a_fresh_node(live, kanban_home, svc)
    result = _round(kanban_home, PagedHub(svc, ME))
    assert result.error is None
    assert not result.behind  # RED on main: 20 of 60 pages, behind, the send held at the run start
    assert _cursor(live) > BACKLOG_PAGES  # at the head the round read (its own send moved the Board on)
    assert svc.board(BOARD)["rows"]["task_runs"][uid]["status"] == "running"
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()
    # The rebuild kept only this gateway's partition: another gateway's cards stay in the replica.
    assert not live.execute("SELECT 1 FROM tasks WHERE id LIKE 't_other%'").fetchone()


class SnapshotFailsOnceHub(PagedHub):
    """The first snapshot page answers 503 (a Board blip mid-rebuild); later ones succeed."""

    def __init__(self, svc, gateway):
        super().__init__(svc, gateway)
        self.failed = False

    def snapshot_page(self, board, table, after="", limit=500):
        if not self.failed:
            self.failed = True
            raise ksc.HubError(503, "snapshot unavailable")
        return self._inner.snapshot_page(board, table, after)


def test_an_interrupted_cold_rebuild_is_retried_as_a_rebuild(live, svc, kanban_home):
    """A rebuild cut short must leave the file cold: RED at d15019ab5a, where the first feed page was applied before
    the snapshot, so a failed rebuild left cursor 1 and the next round fell back to the 20-page full replay."""
    _tid, _run_id, uid = _claimed_on_a_fresh_node(live, kanban_home, svc)
    hub = SnapshotFailsOnceHub(svc, ME)
    try:
        first = _round(kanban_home, hub)
    except ksc.HubError:
        first = None
    assert hub.failed
    assert first is None or first.error is not None
    assert _cursor(live) == 0  # still cold: the next round rebuilds again
    second = _round(kanban_home, hub)
    assert second.error is None
    assert not second.behind
    assert _cursor(live) > BACKLOG_PAGES
    assert svc.board(BOARD)["rows"]["task_runs"][uid]["status"] == "running"


def test_a_warm_live_file_behind_the_head_still_pulls_its_budget(live, svc, kanban_home):
    """Control: only a cursor-0 file rebuilds; a warm file behind the head drains the feed over rounds (its run
    rows held meanwhile, test_kanban_sync_push_starvation)."""
    _other_creates(svc, 1)
    assert _round(kanban_home, svc.hub(ME)).error is None  # warm: at the head
    start = _cursor(live)
    assert start
    _other_creates(svc, BACKLOG_PAGES)
    result = _round(kanban_home, PagedHub(svc, ME))
    assert result.behind and _cursor(live) == start + backlog.PULL_PAGE_BUDGET


def test_a_rate_limited_requeue_frees_the_lease_and_the_same_gateway_reclaims(live, svc, kanban_home):
    """Defect 2 (lease_held holder=self for the whole TTL) is defect 1: the Board frees a lease when its holder's
    release (or status-leaving write) arrives, and those waited behind the run start. With the outbox moving, the
    neutral release lands, the Board counts no failure, and this gateway's next claim is granted."""
    tid, run_id, uid = _claimed_on_a_fresh_node(live, kanban_home, svc)
    dead = dispatch._DeadWorker("rate_limited", 75, "quota", "rate_limited", {"retry_status": "ready"},
                                rate_limited=True)
    dead.run_id = run_id
    with kbc.write_txn(live):
        row = live.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert neutral.queue(live, row, dead)
    hub = PagedHub(svc, ME)
    assert not _round(kanban_home, hub).behind
    board = svc.board(BOARD)
    assert tid not in board["leases"]  # RED on main: held until the Board's TTL
    assert board["rows"]["task_runs"][uid]["outcome"] == "rate_limited"
    assert not board["rows"]["tasks"][tid].get("consecutive_failures")
    _round(kanban_home, hub)  # the Board's release reaches the live file
    assert kb.get_task(live, tid).status == "ready"
    # The same gateway's next grant (a new run) is not deferred lease_held holder=self.
    lease = ksc.HubLeaseClient(svc.hub(ME)).acquire(board_id=BOARD, task_uid=tid, run_uid=sync.uuid7(),
                                                     gateway_id=ME, ttl_s=900)
    assert lease.granted, (lease.reason, lease.holder)
