"""A foreign copy adopted here as an origin owner stays, and one copy that cannot go never stops the pickup.

worker-4 2026-11-13 (t_01a1d4f144e2f2a0): a card created on the macbook was adopted on worker-4
(``kanban_origin_adopt``) and later archived. ``evict`` picked the archived foreign copy, its
``origin_creation_intents`` row (``task_id REFERENCES tasks(id)``) made ``DELETE FROM tasks`` raise
``FOREIGN KEY constraint failed``, the whole eviction rolled back and ``pickup`` raised before importing
anything, every tick for hours; cards pinned to worker-4 were never imported."""
import logging

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_origin_creation as creation
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]


def _status(conn, tid):
    t = kb.get_task(conn, tid)
    return t.status if t else None


def _copy(hub, worker, **kw):
    """A hub card copied into the worker's file (not claimed, not running)."""
    tid = support._hub_card(hub, runs_on=PIN, **kw)
    worker.round()
    worker.enter()
    replica = wn.open_replica(worker.conn)
    try:
        assert wn.import_cards(worker.conn, replica, [tid]) == [tid]
    finally:
        replica.close()
    return tid


def _adopt(worker, tid):
    worker.enter()
    creation.adopt_with_origin(worker.conn, {
        "request_id": f"adopt:{tid}:local-origin",
        "owner": {"gateway": support.WORKER, "profile": "default", "board": support.BOARD, "task_id": tid},
        "origin": {"gateway": "macbook", "profile": "default", "session": "local-origin"}}, board=support.BOARD)
    worker.round()  # push the adoption's synced event so only the intent row could keep the copy


def _archive_on_hub(hub, worker, tid):
    hub.enter()
    assert kb.archive_task(hub.conn, tid)
    hub.round()
    worker.round()
    worker.enter()
    assert _status(worker.conn, tid) == "archived"


def _intents(conn, tid):
    return conn.execute("SELECT COUNT(*) FROM origin_creation_intents WHERE task_id = ?", (tid,)).fetchone()[0]


def _no_failed_evictions(caplog):
    # Kept by the query, never attempted: a failed DELETE would be caught by the per-copy savepoint and logged.
    assert not [r for r in caplog.records if "could not evict" in r.getMessage()]


def test_an_adopted_archived_copy_stays_and_pickup_still_imports(gws, caplog):
    hub, worker = gws
    adopted = _copy(hub, worker, title="adopted here, then archived")
    _adopt(worker, adopted)
    _archive_on_hub(hub, worker, adopted)
    plain = _copy(hub, worker, title="plain foreign copy")
    _archive_on_hub(hub, worker, plain)
    with caplog.at_level(logging.WARNING, logger=wn._log.name):
        assert wn.evict(worker.conn) == [plain]  # the intent row's card stays; the rest still goes
    _no_failed_evictions(caplog)
    assert _status(worker.conn, adopted) == "archived" and _intents(worker.conn, adopted) == 1
    pinned = support._hub_card(hub, title="pinned to the worker", runs_on=PIN)
    worker.round()
    worker.tick()
    assert _status(worker.conn, pinned) == "running"
    assert _status(worker.conn, adopted) == "archived" and _intents(worker.conn, adopted) == 1


@pytest.mark.parametrize("status", ["ready", "blocked", "done"])
def test_an_adopted_copy_is_kept_in_every_unclaimed_state(gws, status, caplog):
    hub, worker = gws
    tid = _copy(hub, worker)
    _adopt(worker, tid)
    worker.conn.execute("UPDATE kanban_sync_state SET applying = 1")
    worker.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    worker.conn.execute("UPDATE kanban_sync_state SET applying = 0")
    worker.conn.commit()
    with caplog.at_level(logging.WARNING, logger=wn._log.name):
        assert wn.evict(worker.conn) == [] and _status(worker.conn, tid) == status
    _no_failed_evictions(caplog)


def test_a_copy_that_cannot_be_deleted_is_kept_and_the_others_go(gws, caplog):
    """Any FK child evict does not know (here a stand-in table) keeps that one copy, logged by id."""
    hub, worker = gws
    stuck = _copy(hub, worker, title="has an unknown FK child")
    other = _copy(hub, worker, title="evictable")
    worker.conn.execute("CREATE TABLE fixture_fk_child (task_id TEXT NOT NULL REFERENCES tasks(id))")
    worker.conn.execute("INSERT INTO fixture_fk_child VALUES (?)", (stuck,))
    worker.conn.commit()
    with caplog.at_level(logging.WARNING, logger=wn._log.name):
        assert wn.evict(worker.conn) == [other]
    assert _status(worker.conn, stuck) == "ready"
    assert kb.get_task(worker.conn, other) is None
    assert any(stuck in r.getMessage() for r in caplog.records)
    # The kept copy's own rows are intact (its savepoint rolled back, not the outer eviction).
    assert worker.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (stuck,)).fetchone()[0] > 0
    assert worker.conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0] == 0


def test_pickup_goes_on_when_eviction_raises(gws, monkeypatch, caplog):
    hub, worker = gws
    pinned = support._hub_card(hub, title="pinned", runs_on=PIN)

    def boom(conn):
        raise RuntimeError("evict failed")
    monkeypatch.setattr(wn, "evict", boom)
    worker.round()
    with caplog.at_level(logging.WARNING, logger=wn._log.name):
        worker.tick()
    assert _status(worker.conn, pinned) == "running"
    assert any("evicting finished copies failed" in r.getMessage() for r in caplog.records)
