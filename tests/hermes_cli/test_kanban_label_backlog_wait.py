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
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'created'", (created_at, tid))
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


def test_ready_hint_preserves_busy_pool_advice(hub):
    tid = _seed(hub.conn, claims=1)
    hint = kpu.ready_hint(hub.conn, kb.get_task(hub.conn, tid))
    assert hint is not None
    assert "Not a placement problem, do not re-place" in hint, hint
    assert "re-place on a capable gateway" not in hint, hint


def test_predecessors_must_have_exact_labels(hub):
    from hermes_cli import kanban_label_backlog as klb
    tid = _seed(hub.conn, claims=0)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET runs_on=? WHERE title=?", (json.dumps(["pool:worker"]), "older 0"))
        hub.conn.execute("UPDATE tasks SET runs_on=? WHERE title=?",
                         (json.dumps([*LABELS, "machine:worker-3"]), "older 1"))
    backlog = klb.read(hub.conn, tid, LABELS, machines={"worker-3"})
    assert backlog["known"] and backlog["ahead"] == 0, backlog


def test_target_claim_in_same_second_as_ready_episode_is_not_pool_evidence(hub, monkeypatch):
    from hermes_cli import kanban_label_backlog as klb
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    tid = _seed(hub.conn, claims=0)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "claimed", {"holder": "worker-3"})
        kb._append_event(hub.conn, tid, "continued", {"status": "ready"})
    # Timestamp filtering alone cannot distinguish the previous claim in this second.
    backlog = klb.read(hub.conn, tid, LABELS, machines={"worker-3"}, now=now)
    assert backlog["known"] and backlog["claimed_recent"] == 0, backlog
    assert backlog["claimers"] == [] and not klb.busy(backlog), backlog
    assert klb.move_refusal(hub.conn, tid, "ready", LABELS, ["pool:worker"], machines=POOL, now=now) is None


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


@pytest.mark.parametrize("priority,ahead", [(100, 2), (1000, 3)])
def test_claim_duplicates_count_once_and_priority_donation_orders_queue(hub, priority, ahead):
    from hermes_cli import kanban_label_backlog as klb
    tid = _seed(hub.conn, claims=1)
    claimed = hub.conn.execute("SELECT id FROM tasks WHERE title='claimed 0'").fetchone()[0]
    later = _card(hub.conn, "later but urgent", int(time.time()))
    waiter = _card(hub.conn, "urgent waiter", int(time.time()))
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, claimed, "claimed", {"holder": "worker-3"})
        hub.conn.execute("UPDATE tasks SET priority=?, status='todo' WHERE id=?", (priority, waiter))
        hub.conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (later, waiter))
    backlog = klb.read(hub.conn, tid, LABELS, machines={"worker-3"})
    # Thirty-one minutes of age beats donated 100, but not donated 1000.
    assert backlog["ahead"] == ahead and backlog["claimed_recent"] == 1
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


@pytest.mark.parametrize("case", ["empty_labels", "missing_task", "unreadable"])
def test_unknown_evidence_cannot_refuse_a_move(hub, case):
    import sqlite3
    from hermes_cli import kanban_label_backlog as klb
    tid = _seed(hub.conn, claims=1)
    conn = sqlite3.connect(":memory:") if case == "unreadable" else hub.conn
    try:
        result = klb.read(conn, "missing" if case == "missing_task" else tid,
                          [] if case == "empty_labels" else LABELS, machines={"worker-3"})
        assert result["known"] is False and not klb.busy(result)
        assert klb.words(result) == ""
    finally:
        if conn is not hub.conn:
            conn.close()


@pytest.mark.parametrize("raw", [None, {}, [], "not json", '[1]', '["pool:worker", null]'])
def test_malformed_labels_are_not_evidence(raw):
    from hermes_cli import kanban_label_backlog as klb
    assert klb._labels(raw) is None


@pytest.mark.parametrize("raw", [None, [], "not json", '{"holder": 3}', '{"holder": ""}'])
def test_malformed_holders_are_not_evidence(raw):
    from hermes_cli import kanban_label_backlog as klb
    assert klb._holder(raw) is None


def test_hub_update_hold_does_not_hide_recent_pool_claims(hub):
    """This host's update hold never stops a pool card it cannot lease: the busy-pool reason stays
    (t_01a1e01b9645d70c; this test used to assert the reverse)."""
    tid = _seed(hub.conn, claims=1)
    assert kpu.post_notice(hub.conn, tid, tid, wait_key="hold", waited_seconds=1900, ready_seconds=1900,
                           kind=kcw.NOTICE_KIND, extra={"core_update_hold": True, "core_update_started": "x"})
    raw = hub.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                           (tid, kcw.NOTICE_KIND)).fetchone()[0]
    payload = json.loads(raw)
    assert payload["admission"] == {"reason": "matching_pool_busy", "status": "queued"}, payload
    assert not any(k.startswith("core_update_") for k in payload), payload
    text = kcw.notice_text(payload)
    assert "claims are held for a pending Hermes" not in text
    assert "Not a placement problem" in text


@pytest.mark.parametrize("transition", ["continued", "shared_blocker_wake", "clear_already_ready"])
def test_claims_before_current_ready_episode_do_not_veto_capability_drop(hub, monkeypatch, transition):
    from hermes_cli import kanban_label_backlog as klb
    now = int(time.time())
    # The ready-episode event is stamped by the event writer's clock. Pinned, it starts exactly at ``now``, so the
    # final claim at ``now`` is inside the episode; unpinned, a second boundary before the write excluded it.
    monkeypatch.setattr(time, "time", lambda: now)
    tid = _seed(hub.conn, claims=1)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE task_events SET created_at=? WHERE kind='claimed'", (now - 60,))
        if transition == "shared_blocker_wake":
            from hermes_cli import kanban_wake_check as kwc
            hub.conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
            assert kwc._wake(hub.conn, tid, "shared_blocker_cleared", {"blocker_id": "quota"})
        elif transition == "clear_already_ready":
            kb._append_event(hub.conn, tid, "shared_blocker_cleared", {"blocker_id": "quota"})
        else:
            kb._append_event(hub.conn, tid, "continued", {"status": "ready"})
    result = klb.read(hub.conn, tid, LABELS, machines={"worker-3"}, now=now)
    expected = 1 if transition == "clear_already_ready" else 0
    assert result["known"] and result["claimed_recent"] == expected
    refusal = klb.move_refusal(hub.conn, tid, "ready", LABELS, ["pool:worker"], machines=POOL, now=now)
    assert bool(refusal) == bool(expected)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE task_events SET created_at=? WHERE kind='claimed'", (now,))
    assert klb.move_refusal(hub.conn, tid, "ready", LABELS, ["pool:worker"], machines=POOL, now=now)
