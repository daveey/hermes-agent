"""An ask's ``resolved_check`` keeps exactly one live probe on a synced board: on the gateway that filed the ask. [daveey]

``task_wake_checks`` is machine-local and never syncs. A worker node that runs another gateway's card and files a
human ask on it keeps its blocked copy while it runs the probe (``kanban_worker_node.evict``), and its unblock is a
status write the Board carries home. The home board never starts another gateway's ask probe. A board that loses
the probe of an ask it filed re-registers it once; a second loss comments once and stops. A probe that exhausted
its failures is never resurrected by that restore. A relayed ask whose probe host stops reporting gets one
comment on the home board, never an unblock and never a second probe there.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_wake_check as kwc
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_human_request import validate_human_request
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, WORKER, Gateway

ASK = {"action": "Press the pair button on the watch. Unblock = finish pairing; archive = drop the watch.",
       "why_human": "physical button on David's device", "interaction": "physical",
       "scope": "watch pairing", "evidence": "hub shows the watch unpaired"}


def _probe(flag: Path) -> str:
    return f"test -f {flag} && cat {flag} || exit 1"


def _ask(cmd=None, every=300):
    return validate_human_request({**ASK, **({"resolved_check": cmd, "resolved_check_every": every} if cmd else {})})


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _status(conn, tid):
    task = kb.get_task(conn, tid)
    return task.status if task else None


def _settle():
    deadline = time.monotonic() + 30
    while kwc.in_flight() and time.monotonic() < deadline:
        time.sleep(0.05)


@pytest.fixture
def gws(tmp_path, monkeypatch):
    svc = FakeService(lease_runs=True)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    sync.consume_dispatcher_wake()
    home = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield home, worker
    kwc._shutdown_for_tests()
    for g in (home, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _relayed_ask(home, worker, request):
    """Home creates a card, the worker runs it, files a human ask on it and pushes the block."""
    home.enter()
    tid = kb.create_task(home.conn, title="runs on the worker", assignee="default", runs_on=["linux"])
    home.round()
    worker.round()
    worker.tick()
    assert _status(worker.conn, tid) == "running"
    run_id = kb.get_task(worker.conn, tid).current_run_id
    assert kb.block_task(worker.conn, tid, reason="need the button", kind="needs_input",
                         expected_run_id=run_id, human_request=request)
    worker.round()
    return tid


def test_a_relayed_ask_keeps_one_live_probe_and_its_unblock_reaches_the_home_board(gws, tmp_path):
    home, worker = gws
    flag = tmp_path / "flag"
    tid = _relayed_ask(home, worker, _ask(_probe(flag)))
    assert _events(worker.conn, tid, "blocked")[-1].payload["probe_host"] == WORKER
    worker.tick()   # pickup evicts finished copies; the sweep runs
    worker.tick()
    _settle()
    assert _status(worker.conn, tid) == "blocked", "the worker evicted its copy, and with it the only ask probe"
    state = kwc.get_state(worker.conn, tid)
    assert state is not None and state["purpose"] == kar.PURPOSE and state["cmd"] == _probe(flag)

    home.round()
    assert _status(home.conn, tid) == "blocked"
    assert _events(home.conn, tid, "blocked")[-1].payload["probe_host"] == WORKER
    home.enter()
    kwc.run_due_wake_checks(home.conn, now=int(time.time()) + 400, wait=True)
    assert kwc.get_state(home.conn, tid) is None                     # exactly one probe: the worker's
    assert not _events(home.conn, tid, kar.RESTORED_EVENT) and not _events(home.conn, tid, kar.LOST_EVENT)

    flag.write_text("watch paired\n")
    worker.enter()
    kwc.run_due_wake_checks(worker.conn, now=int(time.time()) + 301, wait=True)
    assert _status(worker.conn, tid) == "ready"
    worker.round()
    home.round()
    assert _status(home.conn, tid) == "ready"
    assert "watch paired" in _events(home.conn, tid, kar.EVENT)[-1].payload["output"]


def test_a_relayed_ask_without_resolved_check_is_still_evicted(gws, tmp_path):
    home, worker = gws
    tid = _relayed_ask(home, worker, _ask())
    worker.tick()
    worker.tick()
    assert _status(worker.conn, tid) is None
    home.round()
    assert _status(home.conn, tid) == "blocked"


def test_a_relayed_ask_probe_reports_alive_to_the_home_board(gws, tmp_path):
    home, worker = gws
    tid = _relayed_ask(home, worker, _ask(_probe(tmp_path / "flag")))
    worker.enter()
    kwc.run_due_wake_checks(worker.conn, now=int(time.time()) + 301, wait=True)   # just filed: no alive note
    assert _events(worker.conn, tid, kar.ALIVE_EVENT) == []
    kwc.run_due_wake_checks(worker.conn, now=int(time.time()) + kwc.ALIVE_EVERY + 601, wait=True)
    assert _events(worker.conn, tid, kar.ALIVE_EVENT)[-1].payload["host"] == WORKER
    worker.round()
    home.round()
    assert _events(home.conn, tid, kar.ALIVE_EVENT)


def test_the_home_board_comments_once_when_a_relayed_asks_probe_host_went_silent(gws, tmp_path):
    home, worker = gws
    flag = tmp_path / "flag"
    tid = _relayed_ask(home, worker, _ask(_probe(flag)))   # the worker never probes again
    home.round()
    home.enter()
    now = int(time.time())
    assert kar.reconcile(home.conn, now=now + kwc.ALIVE_EVERY)["silent"] == []
    silent = now + kwc.ALIVE_EVERY + 2 * 300 + kwc.SILENT_GRACE + 5
    assert kar.reconcile(home.conn, now=silent)["silent"] == [tid]
    assert kar.reconcile(home.conn, now=silent + 3600)["silent"] == []          # once
    assert _status(home.conn, tid) == "blocked"                                  # never unblocks for David
    assert kwc.get_state(home.conn, tid) is None                                 # never probes on the wrong host
    ev = _events(home.conn, tid, kar.SILENT_EVENT)
    assert len(ev) == 1 and ev[0].payload["probe_host"] == WORKER
    notes = [c.body for c in kb.list_comments(home.conn, tid) if "has not reported" in c.body]
    assert len(notes) == 1 and WORKER in notes[0] and _probe(flag) in notes[0]
    # The worker still runs its probe, and its exit 0 still resumes the card everywhere.
    flag.write_text("watch paired\n")
    worker.enter()
    kwc.run_due_wake_checks(worker.conn, now=int(time.time()) + 301, wait=True)
    worker.round()
    home.round()
    assert _status(home.conn, tid) == "ready"


# --- one board: lost, restored, exhausted ------------------------------------------------------------------------

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn
    kwc._shutdown_for_tests()


def _blocked(conn, cmd, every=120):
    return kb.create_task(conn, title="pair watch", assignee="worker", initial_status="blocked",
                          human_request=_ask(cmd, every))


def _lose(conn, tid):
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))


def test_a_lost_ask_probe_is_restored_once_due_now_then_a_second_loss_comments_once(board, tmp_path):
    flag = tmp_path / "flag"
    tid = _blocked(board, _probe(flag))
    _lose(board, tid)
    now = int(time.time())
    kwc.run_due_wake_checks(board, now=now, wait=True)
    restored = _events(board, tid, kar.RESTORED_EVENT)
    assert len(restored) == 1 and restored[0].payload["cmd"] == _probe(flag)
    state = kwc.get_state(board, tid)
    assert state["purpose"] == kar.PURPOSE and state["last_rc"] == 1   # due at once: it already ran
    kwc.run_due_wake_checks(board, now=now + 1, wait=True)
    assert len(_events(board, tid, kar.RESTORED_EVENT)) == 1             # idempotent while present

    _lose(board, tid)
    for i in range(3):
        kwc.run_due_wake_checks(board, now=now + 10 + i, wait=True)
    assert _status(board, tid) == "blocked"
    assert kwc.get_state(board, tid) is None
    assert len(_events(board, tid, kar.LOST_EVENT)) == 1
    notes = [c.body for c in kb.list_comments(board, tid) if "went missing twice" in c.body]
    assert len(notes) == 1 and _probe(flag) in notes[0]


def test_an_exhausted_probe_is_never_restored_until_the_ask_is_refiled(board, tmp_path):
    script = tmp_path / "probe.sh"
    script.write_text("echo kaput; exit 2\n")
    tid = _blocked(board, f"sh {script}")
    now = int(time.time())
    for i in range(kwc.BROKEN_AFTER):
        kwc.run_due_wake_checks(board, now=now + 200 * (i + 1), wait=True)
    assert len(_events(board, tid, kar.BROKEN_EVENT)) == 1 and kwc.get_state(board, tid) is None
    for i in range(3):
        kwc.run_due_wake_checks(board, now=now + 5000 + 200 * i, wait=True)
    assert kwc.get_state(board, tid) is None
    assert not _events(board, tid, kar.RESTORED_EVENT) and not _events(board, tid, kar.LOST_EVENT)
    assert _status(board, tid) == "blocked" and not _events(board, tid, kar.EVENT)

    # Re-filing with a fixed probe (metadata-only) starts it again.
    from hermes_cli.kanban_human_request_migration import classify_parked_request
    script.write_text("exit 1\n")
    assert classify_parked_request(board, tid, human_request=_ask(f"sh {script}", 120), actor="op",
                                   reason="fixed probe")
    assert kwc.get_state(board, tid)["purpose"] == kar.PURPOSE


def test_a_row_whose_stored_ask_changed_follows_the_ask(board, tmp_path):
    """A synced write (or any writer that bypasses ``store_request``) changed the stored ask: the row follows it."""
    a, b = _probe(tmp_path / "a"), _probe(tmp_path / "b")
    tid = _blocked(board, a)
    import json
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET human_request = ? WHERE id = ?", (json.dumps(_ask(b, 600)), tid))
    kar.reconcile(board)
    state = kwc.get_state(board, tid)
    assert state["cmd"] == b and state["every"] == 600
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET human_request = ? WHERE id = ?", (json.dumps(_ask()), tid))
    kar.reconcile(board)
    assert kwc.get_state(board, tid) is None
    # A stale result for the old command never unblocks the card.
    (tmp_path / "a").write_text("x")
    assert kar.apply_result(board, tid, a, 0, "x", generation="g") is None
    assert _status(board, tid) == "blocked"


def test_a_permanently_stopped_probe_takes_no_write_txn(board, tmp_path, monkeypatch):
    script = tmp_path / "probe.sh"
    script.write_text("echo kaput; exit 2\n")
    broken = _blocked(board, f"sh {script}")
    now = int(time.time())
    for i in range(kwc.BROKEN_AFTER):
        kwc.run_due_wake_checks(board, now=now + 200 * (i + 1), wait=True)
    lost = _blocked(board, "exit 1")
    for _ in range(2):
        _lose(board, lost)
        kar.reconcile(board, now=now)
    assert _events(board, broken, kar.BROKEN_EVENT) and _events(board, lost, kar.LOST_EVENT)

    writes = []
    real = kb.write_txn
    monkeypatch.setattr(kb, "write_txn", lambda conn, *a, **k: writes.append(1) or real(conn, *a, **k))
    for i in range(3):
        out = kar.reconcile(board, now=now + 10_000 + i)
        assert not any(out.values()), out
    assert writes == []
    restorable = _blocked(board, "exit 1")
    _lose(board, restorable)
    writes.clear()
    assert kar.reconcile(board, now=now + 20_000)["restored"] == [restorable] and len(writes) == 1
