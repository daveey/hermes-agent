"""A ready card pinned ``machine:<this host>`` that the host never imports gets pickup evidence and agent repair work.

t_01a1c9097f6a94bc (2026-11-11): t_01a1c81a5323b6d1 sat on the Board ready and pinned to ``machine:hub`` for 23 min
while the hub neither imported nor claimed it, and nothing recorded why or alerted. Pickup now records, per pinned
candidate it does not import, the reason (deduped per card and reason); the dispatcher's sweep opens ONE agent repair
card for a card stalled past ``kanban.sync.pickup_stall_seconds`` (default 600 s), and ONE per host while the queue is
simply full. Same fake-Board two-gateway harness as ``test_kanban_worker_node``.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

WORKER, HUB = support.WORKER, support.HUB
DETECTOR = "pickup-stall-detector"
svc = support.svc
gws = support.gws


def _pinned(hub, title="pinned to the worker", to=WORKER):
    return support._hub_card(hub, title=title, runs_on=[f"machine:{to}"])


def _age(worker, tid, seconds):
    """Make the replica say the card became ready ``seconds`` ago (the Board's ``ready_since`` and creation)."""
    path = wn._replica_path(worker.conn)
    with sqlite3.connect(path) as replica:
        replica.execute("UPDATE tasks SET ready_since = COALESCE(ready_since, created_at * 1000) - ?, "
                        "created_at = created_at - ? WHERE id = ?", (seconds * 1000, seconds, tid))


def _repairs(conn):
    return conn.execute("SELECT * FROM tasks WHERE created_by = ? ORDER BY created_at, id", (DETECTOR,)).fetchall()


def _no_profiles(monkeypatch):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: name != "default"))


def test_a_pinned_card_left_unpicked_past_the_threshold_gets_one_repair_card(gws, monkeypatch):
    hub, worker = gws
    _no_profiles(monkeypatch)  # the worker has no `default` profile, so pickup skips the card every pass
    tid = _pinned(hub)
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None  # not imported
    assert not _repairs(worker.conn)  # not stalled long enough yet
    _age(worker, tid, 601)
    worker.tick()
    [repair] = _repairs(worker.conn)
    assert repair["human_request"] is None
    assert tid in repair["body"] and "profile_ineligible" in repair["body"]
    assert repair["status"] in ("ready", "running")
    worker.tick()
    assert len(_repairs(worker.conn)) == 1  # a second tick reuses the open card


def test_the_skip_reason_is_recorded_once_per_reason_and_shown_on_the_target_host(gws, monkeypatch):
    hub, worker = gws
    _no_profiles(monkeypatch)
    tid = _pinned(hub)
    worker.round()
    for _ in range(3):
        worker.tick()
    from hermes_cli import kanban_pickup_stall as kps
    rows = worker.conn.execute("SELECT reason FROM kanban_pickup_skips WHERE task_id = ?", (tid,)).fetchall()
    assert [r["reason"] for r in rows] == ["profile_ineligible"]
    last = kps.latest(worker.conn, tid)
    assert last["reason"] == "profile_ineligible" and last["first_at"] <= last["last_at"]
    worker.enter()
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_show({"task_id": tid}))
    assert out["source"] == "replica"
    assert out["pickup_skip"]["reason"] == "profile_ineligible"


@pytest.mark.parametrize("closed,closed_ago,want", [
    (None, 0, 1),             # open: reused
    ("done", 60, 1),          # just finished: not handed straight back
    ("done", 3601, 2),        # finished over an hour ago and still stalled: replaced
    ("archived", 3601, 2),
])
def test_an_open_or_recent_repair_is_reused_and_an_old_closed_one_replaced(gws, monkeypatch, closed, closed_ago,
                                                                            want):
    hub, worker = gws
    _no_profiles(monkeypatch)
    tid = _pinned(hub)
    worker.round()
    worker.tick()
    _age(worker, tid, 601)
    worker.tick()
    [first] = _repairs(worker.conn)
    if closed:
        with kb.write_txn(worker.conn):
            worker.conn.execute("UPDATE tasks SET status = ?, completed_at = CAST(strftime('%s','now') AS INTEGER) "
                                "- ?, created_at = created_at - ? WHERE id = ?",
                                (closed, closed_ago, closed_ago, first["id"]))
    worker.tick()
    assert len(_repairs(worker.conn)) == want


def test_a_held_machine_files_one_hold_card_not_one_per_card(gws):
    hub, worker = gws
    a = _pinned(hub, title="a")
    b = _pinned(hub, title="b")
    worker.round()
    worker.enter()
    machines = {**support.MACHINES, WORKER: {**support.MACHINES[WORKER], "hold": True}}
    wn.set_registry_fetch(lambda: {"me": WORKER, "machines": list(machines.values())})
    wn._REGISTRY.update(at=None, value=None, retry=False)
    wn.pickup(worker.conn, budget=4)
    _age(worker, a, 900)
    _age(worker, b, 700)
    from hermes_cli import kanban_pickup_stall as kps
    kps.sweep(worker.conn)
    kps.sweep(worker.conn)
    [card] = _repairs(worker.conn)
    assert "held" in card["title"].lower() and a in card["title"] and b in card["body"]
    assert "do not lift it" in card["body"] and card["priority"] == -1


def test_a_refused_card_is_not_hidden_behind_a_full_queue(gws, monkeypatch):
    """budget_exhausted only for cards a slot would start: a capped tick still names a profile refusal."""
    hub, worker = gws
    _no_profiles(monkeypatch)
    tid = _pinned(hub)
    worker.round()
    worker.tick(max_spawn=0)
    from hermes_cli import kanban_pickup_stall as kps
    assert kps.latest(worker.conn, tid)["reason"] == "profile_ineligible"


def test_a_card_imported_in_time_raises_nothing(gws):
    hub, worker = gws
    tid = _pinned(hub)
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid).status == "running"
    _age(worker, tid, 601)
    worker.tick()
    assert not _repairs(worker.conn)


def test_a_card_pinned_to_another_host_raises_nothing_here(gws, monkeypatch):
    hub, worker = gws
    tid = _pinned(hub, to=HUB)
    worker.round()
    worker.tick()
    _age(worker, tid, 601)
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None
    assert not _repairs(worker.conn)
    from hermes_cli import kanban_pickup_stall as kps
    assert kps.latest(worker.conn, tid) is None  # nothing recorded for a card pinned elsewhere


def test_a_full_queue_files_one_card_per_host_naming_the_oldest(gws):
    hub, worker = gws
    old = _pinned(hub, title="oldest")
    new = _pinned(hub, title="newer")
    worker.round()
    worker.tick(max_spawn=0)  # at capacity: pickup never runs, the capped path records why
    from hermes_cli import kanban_pickup_stall as kps
    assert kps.latest(worker.conn, old)["reason"] == "budget_exhausted"
    assert kps.latest(worker.conn, new)["reason"] == "budget_exhausted"
    _age(worker, old, 900)
    _age(worker, new, 700)
    worker.tick(max_spawn=0)
    worker.tick(max_spawn=0)
    [card] = _repairs(worker.conn)
    assert old in card["title"] and "queue" in card["title"].lower()
    assert "budget_exhausted" in card["body"] and new in card["body"]


def test_a_stall_with_another_last_reason_gets_its_own_card_beside_the_queue_card(gws):
    hub, worker = gws
    full = _pinned(hub, title="waits for a slot")
    odd = _pinned(hub, title="refused here")
    worker.round()
    worker.tick(max_spawn=0)
    from hermes_cli import kanban_pickup_stall as kps
    kps.record(worker.conn, {odd: ("capability_missing", "labels_mismatch")})  # a later pass refused it
    assert kps.latest(worker.conn, odd)["reason"] == "capability_missing"
    _age(worker, full, 700)
    _age(worker, odd, 900)
    worker.tick(max_spawn=0)  # the sweep runs before the capped record, so it sees the refusal
    repairs = _repairs(worker.conn)
    assert len(repairs) == 2
    [own] = [r for r in repairs if odd in r["title"]]
    assert "capability_missing" in own["body"] and "labels_mismatch" in own["body"]
    [queue] = [r for r in repairs if r["id"] != own["id"]]
    assert full in queue["title"] and odd not in queue["body"]


def test_the_threshold_is_configurable(gws, monkeypatch):
    hub, worker = gws
    _no_profiles(monkeypatch)
    from hermes_cli import kanban_db_sync as sync
    real = sync.sync_config
    monkeypatch.setattr(sync, "sync_config", lambda: {**real(), "pickup_stall_seconds": 1200})
    tid = _pinned(hub)
    worker.round()
    worker.tick()
    _age(worker, tid, 601)
    worker.tick()
    assert not _repairs(worker.conn)
    _age(worker, tid, 700)
    worker.tick()
    assert len(_repairs(worker.conn)) == 1


def test_a_held_machine_records_hold_and_a_parentless_gap_records_parents_incomplete(gws):
    hub, worker = gws
    held = _pinned(hub, title="held machine")
    parent = support._hub_card(hub, title="parent not done")
    gated = _pinned(hub, title="gated")
    worker.round()
    path = wn._replica_path(worker.conn)
    with sqlite3.connect(path) as replica:  # the Board says ready while a parent is open: refused parents_not_done
        replica.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, gated))
    worker.enter()
    machines = {**support.MACHINES, WORKER: {**support.MACHINES[WORKER], "hold": True}}
    wn.set_registry_fetch(lambda: {"me": WORKER, "machines": list(machines.values())})
    wn._REGISTRY.update(at=None, value=None, retry=False)
    wn.pickup(worker.conn, budget=4)
    from hermes_cli import kanban_pickup_stall as kps
    assert kps.latest(worker.conn, held)["reason"] == "hold"
    assert kps.latest(worker.conn, gated)["reason"] == "parents_incomplete"
    assert kb.get_task(worker.conn, held) is None


def test_records_are_deduped_per_card_and_reason_and_refreshed_on_change(tmp_path):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_pickup_stall as kps
    conn = kbc.connect(tmp_path / "kanban.db")
    t0 = 1_800_000_000.0
    assert kps.record(conn, {"t_a": ("budget_exhausted", "x")}, now=t0) == 1
    assert kps.record(conn, {"t_a": ("budget_exhausted", "x")}, now=t0 + 10) == 0  # same reason, inside refresh
    assert kps.record(conn, {"t_a": ("budget_exhausted", "x")}, now=t0 + 61) == 1
    assert kps.latest(conn, "t_a")["passes"] == 2
    assert kps.record(conn, {"t_a": ("profile_ineligible", "y")}, now=t0 + 62) == 1
    assert kps.latest(conn, "t_a")["reason"] == "profile_ineligible"
    assert kps.record(conn, {"t_a": ("budget_exhausted", "x")}, now=t0 + 62) == 1  # a change is written at once
    assert kps.latest(conn, "t_a")["reason"] == "budget_exhausted"
    assert kps.record(conn, {"t_b": ("bogus", None)}, now=t0) == 1
    assert kps.latest(conn, "t_b")["reason"] == "other"
    assert conn.execute(f"SELECT count(*) FROM {kps.TABLE}").fetchone()[0] == 3


def test_records_are_bounded_by_age_and_count(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_pickup_stall as kps
    conn = kbc.connect(tmp_path / "kanban.db")
    t0 = 1_800_000_000.0
    kps.record(conn, {"t_old": ("hold", None)}, now=t0 - kps.RETENTION_SECONDS - 5)
    kps.record(conn, {f"t_{i}": ("hold", None) for i in range(5)}, now=t0)
    monkeypatch.setattr(kps, "MAX_ROWS", 3)
    kps._prune(conn, int(t0 * 1000))
    ids = {r[0] for r in conn.execute(f"SELECT task_id FROM {kps.TABLE}")}
    assert len(ids) == 3 and "t_old" not in ids


def test_cli_show_names_the_skip_for_a_card_not_imported_here(gws, monkeypatch):
    hub, worker = gws
    _no_profiles(monkeypatch)
    tid = _pinned(hub)
    worker.round()
    worker.tick()
    from hermes_cli import kanban_pickup_stall as kps
    note = kps.not_here_note(worker.conn, tid)
    assert "not imported here" in note and "profile_ineligible" in note
    assert kps.not_here_note(worker.conn, "t_unknown") == ""


def test_a_full_pickup_batch_records_budget_exhausted_for_the_rest(gws):
    hub, worker = gws
    first = _pinned(hub, title="first")
    second = _pinned(hub, title="second")
    worker.round()
    worker.enter()
    imported = wn.pickup(worker.conn, budget=1)
    [left] = [t for t in (first, second) if t not in imported]
    from hermes_cli import kanban_pickup_stall as kps
    assert len(imported) == 1
    assert kps.latest(worker.conn, left)["reason"] == "budget_exhausted"
    assert kps.latest(worker.conn, imported[0]) is None


def test_an_unreachable_registry_is_recorded_as_other(gws):
    hub, worker = gws
    tid = _pinned(hub)
    worker.round()
    worker.enter()

    def down():
        raise OSError("registry down")
    wn.set_registry_fetch(down)
    wn._REGISTRY.update(at=None, value=None, retry=False)
    assert wn.pickup(worker.conn, budget=4) == []
    from hermes_cli import kanban_pickup_stall as kps
    skip = kps.latest(worker.conn, tid)
    assert skip["reason"] == "other" and "registry" in skip["detail"]


@pytest.mark.parametrize("value,want", [(None, 600), (1200, 1200), (0, 600), (-5, 600), ("x", 600), (True, 600)])
def test_stall_seconds_config(monkeypatch, value, want):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_pickup_stall as kps
    monkeypatch.setattr(sync, "sync_config", lambda: {} if value is None else {"pickup_stall_seconds": value})
    assert kps.stall_seconds() == want


def test_a_draining_gateway_files_no_repair(gws, monkeypatch):
    hub, worker = gws
    _no_profiles(monkeypatch)
    tid = _pinned(hub)
    worker.round()
    worker.tick()
    _age(worker, tid, 601)
    from hermes_cli import kanban_gateway_drain
    from hermes_cli import kanban_pickup_stall as kps
    monkeypatch.setattr(kanban_gateway_drain, "dispatch_paused", lambda home=None: True)
    worker.enter()
    assert kps.sweep(worker.conn) == []
    assert _repairs(worker.conn) == []


def test_a_card_whose_import_fails_is_recorded(gws, monkeypatch):
    hub, worker = gws
    tid = _pinned(hub)
    worker.round()
    worker.enter()
    monkeypatch.setattr(wn, "import_cards", lambda conn, replica, ids: [])
    assert wn.pickup(worker.conn, budget=4) == []
    from hermes_cli import kanban_pickup_stall as kps
    assert kps.latest(worker.conn, tid)["detail"] == "import failed"
