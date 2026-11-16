"""Pool wait notices count what the matching workers would claim first, not only identical labels. [daveey]

t_01a1deaf95f5b885's notice said "0 card(s) with matching labels ahead of it" while 33 pool-eligible ready cards
outscored it; the next three claims were exactly the top three of those. ``read(..., machine_labels=...)`` now
also counts ready, unclaimed predecessors (``lane_rows`` order) whose labels a matching worker carries.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_label_backlog as klb
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_db_dispatch_queries import lane_rows
from tests.hermes_cli._placement_helpers import seen_now
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, Gateway

LABELS = ["capability:core-ship", "capability:hub-read", "pool:worker"]
WORKERS = ("worker-1", "worker-2")
POOL = {
    HUB: MACHINES[HUB],
    **{name: {"name": name, "state": "active", "labels": list(LABELS), "pinned_only": False, "max_card_runs": 4,
              "hold": False, "pickup": "all"} for name in WORKERS},
}
CARRIED = {name: wn.effective_labels(POOL[name], name) for name in WORKERS}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    gw = Gateway(tmp_path, HUB, FakeService(lease_runs=True, machines=POOL), monkeypatch, pickup=False)
    gw.enter()
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": seen_now(POOL.values())})
    yield gw
    gw.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _card(conn, title, created_at, runs_on):
    tid = kb.create_task(conn, title=title, assignee="default", runs_on=runs_on)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, tid))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'created'", (created_at, tid))
    return tid


def _older_pool(conn, now, n):
    return [_card(conn, f"pool {i}", now - 3600 + i, ["pool:worker"]) for i in range(n)]


def test_a_pool_eligible_predecessors_are_counted(hub):
    now = int(time.time())
    _older_pool(hub.conn, now, 3)
    tid = _card(hub.conn, "core fix", now - 1900, LABELS)
    backlog = klb.read(hub.conn, tid, LABELS, machines=set(WORKERS), machine_labels=CARRIED)
    assert backlog["known"] and backlog["pool_ahead"] == 3 and backlog["ahead"] == 0, backlog
    assert ("3 ready card(s) ahead that its matching workers can also run; 0 card(s) with matching labels ahead "
            "of it") in klb.words(backlog), klb.words(backlog)


def test_b_labels_no_matching_worker_carries_and_label_less_cards_are_not_counted(hub):
    now = int(time.time())
    _older_pool(hub.conn, now, 1)
    _card(hub.conn, "hub only", now - 3000, ["machine:hub"])
    bare = _card(hub.conn, "no labels", now - 2900, ["pool:worker"])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET runs_on = NULL WHERE id = ?", (bare,))
    tid = _card(hub.conn, "core fix", now - 1900, LABELS)
    ordered = [row[0] for row in lane_rows(hub.conn, "ready")]
    assert ordered.index(bare) < ordered.index(tid)  # the label-less card really is ahead in dispatch order
    backlog = klb.read(hub.conn, tid, LABELS, machines=set(WORKERS), machine_labels=CARRIED)
    assert backlog["pool_ahead"] == 1, backlog


def test_c_newer_and_claimed_cards_are_not_counted(hub):
    now = int(time.time())
    _older_pool(hub.conn, now, 1)
    running = _card(hub.conn, "running", now - 3000, ["pool:worker"])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'running', claim_lock = 'worker-1:1' WHERE id = ?", (running,))
    tid = _card(hub.conn, "core fix", now - 1900, LABELS)
    _card(hub.conn, "newer", now - 60, ["pool:worker"])
    backlog = klb.read(hub.conn, tid, LABELS, machines=set(WORKERS), machine_labels=CARRIED)
    assert backlog["pool_ahead"] == 1, backlog


def test_d_placement_headline_reports_pool_ahead(hub):
    now = int(time.time())
    _older_pool(hub.conn, now, 2)
    claimed = _card(hub.conn, "claimed", now - 4000, LABELS)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (claimed,))
        kb._append_event(hub.conn, claimed, "claimed", {"holder": "worker-1", "run_uid": "r0"})
    tid = _card(hub.conn, "core fix", now - 1900, LABELS)
    place = kqp.placement(hub.conn, tid)
    assert place and place["lane"] == "machine", place
    assert place["backlog"]["pool_ahead"] == 2 and klb.busy(place["backlog"]), place
    text = kqp.headline(place)
    assert ("2 ready card(s) ahead that its matching workers can also run; 0 card(s) with matching labels ahead "
            "of it") in text, text


def test_e_without_machine_labels_output_is_unchanged(hub):
    now = int(time.time())
    _older_pool(hub.conn, now, 2)
    _card(hub.conn, "same labels", now - 2500, LABELS)
    tid = _card(hub.conn, "core fix", now - 1900, LABELS)
    backlog = klb.read(hub.conn, tid, LABELS, machines=set(WORKERS))
    assert "pool_ahead" not in backlog, backlog
    assert klb.words(backlog) == ("1 card(s) with matching labels ahead of it (approximate, this board view); "
                                  "matching workers claimed 0 such card(s) in the last 30 min"), klb.words(backlog)
    assert json.dumps(backlog)  # still JSON-serialisable for notice payloads
