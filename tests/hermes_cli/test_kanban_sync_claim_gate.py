"""A fresh host claims nothing until its first sync round reaches the feed head, and the outbox alarm never ages the
run rows a round holds on purpose (t_01a1d6c4e700958e).

Incident (2026-11-13, autoscaled worker-a1): the first round rebuilt the replica from a full snapshot (cursor 0:
Resync) for ~15 min. The dispatcher leased the scaler's canary during it; the run ended 14 s later, but its rows
waited in the outbox behind the FIFO run-row hold until the end of that round, so the Board kept the lease, the
canary read ``running`` and the enrollment failed. The outbox alarm fired twice meanwhile: the round start had
cleared ``backlog.behind``, so it aged the held run rows.

The Board is the in-memory fake (``kanban_sync_fake_board``) with Board-faithful lease runs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_backlog as backlog
from hermes_cli import kanban_sync_claims as claims
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_outbox_age as outbox_age
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "worker-a1", "hub", "default"


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    """A fresh process: no round reached the head, no loop runs, no alarm verdicts."""
    backlog.reset()
    outbox_age.reset()
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    yield
    backlog.reset()
    outbox_age.reset()
    sync.set_lease_client(None)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def live(kanban_home, svc):
    support.write_routed_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        conn.close()


class SlowHub:
    """The fake Board's hub for this gateway: snapshots of two rows per page and feed pages of one change, calling
    ``during`` once mid-round (on the second non-empty page read)."""

    def __init__(self, svc, during=None):
        self._inner, self.during, self.calls, self.snapshot_pages = svc.hub(ME), during, 0, 0
        self.ident = self._inner.ident

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _maybe(self):
        self.calls += 1
        if self.calls == 2 and self.during is not None:
            self.during()

    def snapshot_page(self, board, table, after="", limit=1000):
        page = self._inner.snapshot_page(board, table, after, limit=2)
        self.snapshot_pages += 1
        if page["rows"]:
            self._maybe()
        return page

    def changes(self, board, since, limit=500):
        page = self._inner.changes(board, since, limit=1)
        if page["changes"]:
            self._maybe()
        return page


def _other_cards(svc, prefix, n):
    hub = svc.hub(OTHER)
    for i in range(n):
        hub.ops(BOARD, [{"client_op_id": f"{prefix}{i}", "op": "upsert", "table": "tasks", "uid": f"t_{prefix}{i:03d}",
                         "hlc": f"{1:013d}-0000-{OTHER}", "fields": {"title": f"{prefix}{i}", "status": "done",
                                                                     "created_at": 1}}])


def _board_history(svc, n):
    """Another gateway's cards, then compaction: a fresh host's cursor 0 is below the retention floor (Resync)."""
    _other_cards(svc, "old", n)
    svc.compact(BOARD)


def _tick(live):
    return kbd.dispatch_once(live, spawn_fn=lambda task, ws, board=None: os.getpid(), board=BOARD)


def _round(kanban_home, hub):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", hub, ME)


def _backlog(svc, prefix, rounds=1):
    """More feed changes than one round's page budget reads (pages of one change): the round ends behind."""
    _other_cards(svc, prefix, rounds * backlog.PULL_PAGE_BUDGET + 5)


# --- the claim gate --------------------------------------------------------------------------------------------

def test_a_fresh_host_claims_nothing_during_its_snapshot_rebuild_round_and_claims_once_at_the_head(
        live, svc, kanban_home, caplog):
    _board_history(svc, 9)
    claims.loop_started()  # this process runs the sync loop (``kanban_sync_stop.run_loop``)
    tid = kb.create_task(live, title="canary", assignee="default")
    seen: dict = {}

    def mid_round():
        seen["tick"] = _tick(live)
        seen["status"] = kb.get_task(live, tid).status
        seen["runs"] = live.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
        seen["lease"] = tid in svc.board(BOARD)["leases"]

    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_sync_claims"):
        hub = SlowHub(svc, mid_round)
        result = _round(kanban_home, hub)
    assert result.error is None and not result.behind
    assert hub.snapshot_pages > 2 and seen  # Resync: a multi-page snapshot rebuild, the tick on its second page
    assert (seen["status"], seen["runs"], seen["lease"]) == ("ready", 0, False)  # RED on main: leased mid-round
    assert seen["tick"].claim_deferred == [(tid, claims.DEFERRAL)]
    assert len([r for r in caplog.records if "claims held" in r.getMessage()]) == 1  # one INFO line per board
    _tick(live)
    assert kb.get_task(live, tid).status == "running"
    assert tid in svc.board(BOARD)["leases"]


def test_a_round_that_ends_behind_the_head_keeps_claims_held(live, svc, kanban_home):
    tid = kb.create_task(live, title="waits", assignee="default")
    assert _round(kanban_home, svc.hub(ME)).error is None  # an earlier process reached the head
    backlog.reset()
    claims.reset()
    claims.loop_started()  # this process: its own first round must reach the head
    _backlog(svc, "new")
    assert _round(kanban_home, SlowHub(svc)).behind
    _tick(live)
    assert kb.get_task(live, tid).status == "ready"
    while _round(kanban_home, SlowHub(svc)).behind:
        pass
    _tick(live)
    assert kb.get_task(live, tid).status == "running"


def test_a_process_without_a_sync_loop_is_not_gated(live):
    """``hermes kanban claim`` from a shell, with no loop recorded anywhere: unchanged."""
    tid = kb.create_task(live, title="operator claim", assignee="default")
    assert kb.claim_task(live, tid, ttl_seconds=900)


def test_a_shadow_board_is_not_gated(live):
    claims.loop_started()
    sync.set_mode(live, "shadow")
    tid = kb.create_task(live, title="shadow", assignee="default")
    assert kb.claim_task(live, tid, ttl_seconds=900)


def test_another_live_process_loop_record_holds_and_opens_claims(live, kanban_home):
    """A profile gateway can hold the dispatcher lock while the root gateway runs the sync loop."""
    tid = kb.create_task(live, title="split", assignee="default")
    path = claims.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    other = os.getppid()  # a live process that is not this one
    path.write_text(json.dumps({"pid": other, "open": {}}))
    assert claims.held(live)
    assert kb.claim_task(live, tid, ttl_seconds=900) is None
    path.write_text(json.dumps({"pid": other, "open": {os.path.realpath(kanban_home / "kanban.db"): BOARD}}))
    assert not claims.held(live)
    path.write_text(json.dumps({"pid": 2 ** 22 + 12345, "open": {}}))  # a dead loop's record: ignored
    assert not claims.held(live)


_PRODUCER = """
import sys
from hermes_cli import kanban_sync_claims as claims
from hermes_cli.kanban_sync_stop import run_loop


def loop(*_):
    print("started", flush=True)  # the loop runs; no round of it has completed
    sys.stdin.readline()
    claims.note_round("default", sys.argv[1], at_head=True)
    print("at-head", flush=True)
    sys.stdin.readline()


run_loop(loop, lambda: True, 1)
print("stopped", flush=True)
"""


def _expect(proc, line):
    ready, _, _ = select.select([proc.stdout], [], [], 60)
    assert ready, f"the producer never printed {line!r}"
    assert proc.stdout.readline().strip() == line


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _send(proc):
    proc.stdin.write("go\n")
    proc.stdin.flush()


def test_a_real_sync_loop_in_another_process_holds_this_processes_claims_until_its_round_reaches_the_head(
        live, kanban_home, tmp_path):
    """The split-process gate with a real producer: another process runs the sync loop (``run_loop``), which must
    publish its held record when the loop starts, before any round. This process has no loop (a profile gateway's
    dispatcher, or ``hermes kanban claim`` from a shell): during that initial sync it claims nothing, once the
    producer's round reaches the head it claims, and when the producer's tenure ends its record goes away."""
    db = kanban_home / "kanban.db"
    tid = kb.create_task(live, title="foreign", assignee="default")
    script = tmp_path / "producer.py"
    script.write_text(_PRODUCER)
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "HERMES_KANBAN_HOME": str(kb.kanban_home()),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(root), os.environ.get("PYTHONPATH")]))}
    producer = subprocess.Popen([sys.executable, "-u", str(script), str(db)], cwd=root, env=env, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        _expect(producer, "started")
        tick = _tick(live)
        assert (tick.claim_deferred, _status(live, tid)) == ([(tid, claims.DEFERRAL)], "ready")
        assert claims.describe(db, "primary") == claims.HELD
        _send(producer)
        _expect(producer, "at-head")
        assert claims.describe(db, "primary") == "open"
        _tick(live)
        assert _status(live, tid) == "running"
        _send(producer)
        _expect(producer, "stopped")
        assert producer.wait(timeout=60) == 0
    finally:
        if producer.poll() is None:
            producer.kill()
            producer.wait(timeout=60)
    assert not claims.state_path().exists()  # the ended tenure removed its record
    assert claims.describe(db, "primary") == "no live sync loop recorded"


@pytest.mark.parametrize("previous_tenure", [False, True])
def test_waiting_sync_gateway_uses_current_lock_owners_record(live, kanban_home, previous_tenure):
    """A watcher waiting for .sync.lock must not override the root loop's readiness."""
    tid = kb.create_task(live, title="split watcher", assignee="default")
    claims.loop_expected()
    assert claims.held(live)  # no owner has finished the initial round
    if previous_tenure:
        from hermes_cli.kanban_sync_stop import run_loop
        run_loop(lambda *_: None, lambda: True, 1)
    path = claims.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.realpath(kanban_home / "kanban.db")
    path.write_text(json.dumps({"pid": os.getppid(), "open": {key: BOARD}}))
    assert not claims.held(live)
    assert claims.describe(kanban_home / "kanban.db", "primary") == "open"
    assert kb.claim_task(live, tid, ttl_seconds=900)


@pytest.mark.parametrize("crash", [False, True])
def test_ended_sync_tenure_discards_its_readiness(live, kanban_home, crash):
    from hermes_cli.kanban_sync_stop import run_loop
    path = kanban_home / "kanban.db"

    def loop(*_):
        claims.note_round(BOARD, path, at_head=True)
        assert not claims.held(live)
        if crash:
            raise RuntimeError("loop failed")

    if crash:
        with pytest.raises(RuntimeError, match="loop failed"):
            run_loop(loop, lambda: True, 1)
    else:
        run_loop(loop, lambda: True, 1)
    assert not claims.state_path().exists()
    assert claims.held(live)
    assert claims.describe(path, "primary") == claims.HELD
    claims.loop_started()
    assert claims.held(live)  # a new tenure must finish its own round


def test_sync_status_shows_the_gate(live, svc, kanban_home, capsys):
    from hermes_cli import kanban_sync_cli
    _board_history(svc, 3)
    claims.loop_started()

    def status():
        kanban_sync_cli._status(argparse.Namespace(board=BOARD, json=True))
        return json.loads(capsys.readouterr().out)["claims"]
    assert status() == claims.HELD
    assert _round(kanban_home, svc.hub(ME)).error is None
    assert status() == "open"


# --- alarm truth -----------------------------------------------------------------------------------------------

def _held_run_row(live, kanban_home, svc):
    """A claimed card whose run row is pending and 20 min old: the row the FIFO hold keeps for the head."""
    tid = kb.create_task(live, title="ran", assignee="default")
    assert _round(kanban_home, svc.hub(ME)).error is None
    assert kb.claim_task(live, tid, ttl_seconds=900)
    old = int((time.time() - 2 * outbox_age.STALE_S) * 1000)
    with kbc.write_txn(live):
        n = live.execute("UPDATE kanban_sync_outbox SET created_at = ? WHERE table_name = 'task_runs' "
                         "AND state = 'pending'", (old,)).rowcount
    assert n
    return tid


def test_before_any_round_reaches_the_head_the_alarm_does_not_age_a_held_run_row(live, svc, kanban_home):
    _held_run_row(live, kanban_home, svc)
    backlog.reset()  # a new process: none of its rounds has reached the head yet
    path = kanban_home / "kanban.db"
    assert outbox_age.oldest_pending_age_s(live, unanswered=True) >= outbox_age.STALE_S  # the row is old
    assert (outbox_age._age(path) or 0) < outbox_age.STALE_S  # RED on main: behind() was False


def test_during_a_round_after_one_that_ended_behind_the_alarm_does_not_age_a_held_run_row(
        live, svc, kanban_home):
    _held_run_row(live, kanban_home, svc)
    path = kanban_home / "kanban.db"
    _backlog(svc, "b", rounds=2)
    assert _round(kanban_home, SlowHub(svc)).behind  # the previous round ended behind the head
    ages: list = []
    assert _round(kanban_home, SlowHub(svc, lambda: ages.append(outbox_age._age(path)))).behind
    assert ages and (ages[0] or 0) < outbox_age.STALE_S  # RED on main: the round start cleared behind()
    assert backlog.due([(BOARD, path)]) == [(BOARD, path)]  # a completed round behind still reruns at once


def test_a_failed_round_neither_reruns_at_once_nor_clears_behind(kanban_home):
    path = kanban_home / "kanban.db"
    backlog.note(path, True)
    backlog.round_started(path)  # the round then raised: nothing completed
    assert backlog.due([(BOARD, path)]) == [] and backlog.behind(path)
    backlog.note(path, False)
    assert not backlog.behind(path)
