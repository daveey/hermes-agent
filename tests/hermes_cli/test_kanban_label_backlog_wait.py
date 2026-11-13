"""A busy same-label pool must not be diagnosed as a placement fault. [daveey]

Fixture: an origin that cannot lease the card, two matching pool workers, two ready predecessors,
and three distinct same-label cards claimed by those workers during the last 30 minutes.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_chat_wait as kcw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_parent_unclaimed as kpu
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli._placement_helpers import seen_now
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, Gateway

LABELS = ["capability:hub-write", "pool:worker"]
POOL = {
    HUB: MACHINES[HUB],
    **{name: {"name": name, "state": "active", "labels": list(LABELS), "pinned_only": False, "max_card_runs": 4,
              "hold": False, "pickup": "all"} for name in ("worker-3", "worker-4")},
}


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


def _card(conn, title, created_at):
    tid = kb.create_task(conn, title=title, assignee="default", runs_on=list(LABELS))
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, tid))
    return tid


def _seed(conn, *, claims: int):
    now = int(time.time())
    for i in range(2):
        _card(conn, f"older {i}", now - 3600 + i)  # ready, same labels, older: ahead of the fix card
    for i in range(claims):
        tid = _card(conn, f"claimed {i}", now - 4000 + i)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "claimed", {"holder": f"worker-{3 + i % 2}", "run_uid": f"r{i}"})
    return _card(conn, "BB CI fix", now - 1900)


def _notice(conn, tid):
    assert kpu.post_notice(conn, tid, tid, wait_key="ready@1", waited_seconds=1900, ready_seconds=1900,
                           parent_title="BB CI fix", kind=kcw.NOTICE_KIND)
    (payload,) = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                              (tid, kcw.NOTICE_KIND)).fetchone()
    payload = json.loads(payload)
    return payload, kcw.notice_text(payload)


def test_busy_pool_notice_reports_queue_place_and_does_not_advise_re_placement(hub):
    tid = _seed(hub.conn, claims=3)
    payload, text = _notice(hub.conn, tid)
    assert "2 card(s) with matching labels ahead of it" in text, text
    assert "matching workers claimed 3 such card(s) in the last 30 min (worker-3, worker-4)" in text, text
    assert "Not a placement problem, do not re-place" in text, text
    assert "re-place on a capable gateway" not in text and "admission reason unknown" not in text, text
    assert payload["admission"] == {"reason": "matching_pool_busy", "status": "queued"}, payload
    assert payload["label_backlog"]["claimed_recent"] == 3 and payload["label_backlog"]["ahead"] == 2, payload
    note = kqp.describe(kqp.placement(hub.conn, tid)) or ""
    assert "claimed 3 such card(s)" in note and "do not re-place it or drop its labels" in note, note


def test_no_recent_same_label_claims_keeps_the_capacity_advice(hub):
    tid = _seed(hub.conn, claims=0)
    payload, text = _notice(hub.conn, tid)
    assert "2 card(s) with matching labels ahead of it" in text, text  # the queue place is still reported
    assert "Not a placement problem" not in text and "re-place on a capable gateway" in text, text
    assert payload["admission"]["reason"] is None, payload


def test_move_dropping_a_capability_while_matching_workers_claim_same_label_cards_is_refused(hub):
    tid = _seed(hub.conn, claims=3)
    res = km.move_queued(hub.conn, tid, runs_on=["pool:worker"], machines=POOL, me=HUB,
                         reason="labels_mismatch for 30 min: no worker advertises capability:hub-write")
    assert res["ok"] is False and res["reason_code"] == "capacity_wait", res
    assert "drops capability:hub-write" in res["error"] and "claimed 3 such card(s)" in res["error"], res
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == LABELS
    forced = km.move_queued(hub.conn, tid, runs_on=["pool:worker"], machines=POOL, me=HUB, force=True,
                            reason="the card needs no hub write")
    assert forced["ok"], forced
    (payload,) = hub.conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'moved'",
                                  (tid,)).fetchone()
    assert "claimed 3 such card(s)" in json.loads(payload)["capacity_wait_overridden"], payload


def test_move_dropping_a_capability_with_no_same_label_claims_is_allowed(hub):
    tid = _seed(hub.conn, claims=0)
    res = km.move_queued(hub.conn, tid, runs_on=["pool:worker"], machines=POOL, me=HUB, reason="no worker has it")
    assert res["ok"], res


@pytest.mark.parametrize("mutation", ["old", "future", "wrong_holder", "local_claim", "extra_label", "moved", "edited"])
def test_ambiguous_or_nonmatching_claim_is_not_capacity_evidence(hub, mutation):
    from hermes_cli import kanban_label_backlog as klb
    tid = _seed(hub.conn, claims=1)
    claimed = hub.conn.execute("SELECT id FROM tasks WHERE title = 'claimed 0'").fetchone()[0]
    with kb.write_txn(hub.conn):
        if mutation in ("old", "future"):
            at = int(time.time()) + (60 if mutation == "future" else -1900)
            hub.conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='claimed'", (at, claimed))
        elif mutation in ("wrong_holder", "local_claim"):
            payload = {"holder": "elsewhere"} if mutation == "wrong_holder" else {"lock": "worker-3:123"}
            hub.conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='claimed'", (json.dumps(payload), claimed))
        elif mutation == "extra_label":
            hub.conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", (json.dumps([*LABELS, "machine:worker-3"]), claimed))
        else:
            kb._append_event(hub.conn, claimed, mutation, {})
    backlog = klb.read(hub.conn, tid, LABELS, machines={"worker-3", "worker-4"})
    assert backlog["known"] and backlog["claimed_recent"] == 0
    assert not klb.busy(backlog)
    assert klb.move_refusal(hub.conn, tid, "ready", LABELS, ["pool:worker"], machines=POOL) is None


@pytest.mark.parametrize("state", ["held", "draining", "inactive", "wrong_labels"])
def test_unavailable_matching_workers_do_not_override_current_holds(hub, state):
    from hermes_cli import kanban_label_backlog as klb
    tid = _seed(hub.conn, claims=1)
    pool = {name: dict(entry) for name, entry in POOL.items()}
    for name in ("worker-3", "worker-4"):
        if state == "held":
            pool[name]["hold"] = True
        elif state == "draining":
            pool[name]["metrics"] = {"drain_active": 1, "at": time.time() * 1000}
        elif state == "inactive":
            pool[name]["state"] = "inactive"
        else:
            pool[name]["labels"] = ["pool:worker"]
    assert not klb.available(LABELS, pool)
    assert klb.move_refusal(hub.conn, tid, "ready", LABELS, ["pool:worker"], machines=pool) is None
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": seen_now(pool.values())})
    payload, text = _notice(hub.conn, tid)
    assert payload["admission"]["reason"] is None
    assert "Not a placement problem" not in text


def test_claim_duplicates_count_once_and_priority_donation_orders_queue(hub):
    from hermes_cli import kanban_label_backlog as klb
    tid = _seed(hub.conn, claims=1)
    claimed = hub.conn.execute("SELECT id FROM tasks WHERE title='claimed 0'").fetchone()[0]
    later = _card(hub.conn, "later but urgent", int(time.time()))
    waiter = _card(hub.conn, "urgent waiter", int(time.time()))
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, claimed, "claimed", {"holder": "worker-3"})
        hub.conn.execute("UPDATE tasks SET priority=100, status='todo' WHERE id=?", (waiter,))
        hub.conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (later, waiter))
    backlog = klb.read(hub.conn, tid, LABELS, machines={"worker-3"})
    assert backlog["ahead"] == 3 and backlog["claimed_recent"] == 1
    assert "approximate, this board view" in klb.words(backlog)


@pytest.mark.parametrize("status,previous,new", [
    ("running", LABELS, ["pool:worker"]),
    ("blocked", LABELS, ["pool:worker"]),
    ("ready", None, ["pool:worker"]),
    ("ready", LABELS, LABELS),
    ("ready", LABELS, ["capability:hub-write"]),
])
def test_move_protection_only_for_ready_capability_drops(hub, status, previous, new):
    from hermes_cli import kanban_label_backlog as klb
    tid = _seed(hub.conn, claims=1)
    assert klb.move_refusal(hub.conn, tid, status, previous, new, machines=POOL) is None
