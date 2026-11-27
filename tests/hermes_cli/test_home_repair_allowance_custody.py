"""Frozen custody on a *second* card must not stall unrelated maintenance (PR4300 findings 1-2).

Expected-correct behaviour, so these fail on the defective head:

* a child's separate repair card in prepared / pending / malformed custody must not abort the readiness pass
  that clears the child's ``parent_missing`` block (the repair note is skipped, the child and healthy
  neighbours still advance);
* the allowance-maintenance sweep must skip a frozen candidate before any Board read or hold, must not abort
  ``start_due``, and must still reach healthy cards on the same page.
The strict SQL fences stay: nothing is written to the frozen card.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_allowance_maintenance as maintenance
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as transfer
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_home_generation_feed as feed_support
from tests.hermes_cli import test_kanban_spend_allowance as fixtures
from tests.hermes_cli import test_kanban_allowance_observation as reads

kanban_home = fixtures.kanban_home
primary = fixtures.primary

NONCE = "pending-destination-0001"
KINDS = ["prepared", "pending", "malformed"]


@pytest.fixture(autouse=True)
def isolated_cursors():
    maintenance._CURSORS.clear()
    yield
    maintenance._CURSORS.clear()


def freeze(conn, task, kind):
    """Put one card into real prepared / pending / malformed-marker custody (existing helpers' recipes)."""
    if kind == "pending":
        with dbc.write_txn(conn):
            conn.execute("UPDATE kanban_sync_state SET gateway_id='hub'")
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0]
        feed_support.feed(conn, task, home_transfer_pending=NONCE, nonce=NONCE)
        with dbc.write_txn(conn):
            conn.execute("UPDATE kanban_sync_state SET applying=1")
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
            conn.execute("UPDATE kanban_sync_state SET applying=0")
        return
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (task,))
        conn.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
        conn.execute("DELETE FROM kanban_sync_outbox")
        transfer.prepare(conn, task, source="worker-a3", destination="hub", nonce="repair-test-nonce-0001",
                         expected_version=7)
    if kind == "malformed":
        with dbc.write_txn(conn):
            conn.execute("DROP TRIGGER home_prepared1_marker_update")
            conn.execute("UPDATE kanban_home_prepared1 SET nonce='invalid',source='' WHERE task_id=?", (task,))


def snapshot(conn, task):
    return [[tuple(r) for r in conn.execute(sql, (task,))] for sql in (
        "SELECT * FROM tasks WHERE id=?", "SELECT * FROM task_events WHERE task_id=? ORDER BY id",
        "SELECT * FROM task_comments WHERE task_id=? ORDER BY id")]


# ── finding 1: frozen repair card ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def detached(primary, monkeypatch):
    """The board is its own authority for parents (the detector's detached mode), so a parent with no row is
    missing rather than merely unknown; custody recipes still need the synced tables."""
    from hermes_cli import kanban_replica_freshness as freshness
    monkeypatch.setattr(freshness, "synced", lambda conn: False)
    return primary


def blocked_child(conn, name="dependent"):
    child = kb.create_task(conn, title=name, assignee="worker")
    with dbc.write_txn(conn):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_late', ?)", (child,))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (child,))
    kb.recompute_ready(conn)
    with dbc.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at=created_at-? WHERE task_id=? AND kind='parent_missing'",
                     (kb.PARENT_MISSING_BLOCK_SECONDS + 1, child))
    kb.recompute_ready(conn)
    task = kb.get_task(conn, child)
    assert (task.status, task.block_kind) == ("blocked", "transient")
    blocked = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (child,)).fetchone()
    return child, json.loads(blocked[0])["repair_task_id"]


def parent_arrives(conn):
    with dbc.write_txn(conn):
        conn.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('t_late','late','done',?)",
                     (int(time.time()),))


def test_control_unfrozen_repair_card_child_reaches_ready(detached):
    primary = detached
    child, repair = blocked_child(primary)
    parent_arrives(primary)
    assert kb.recompute_ready(primary) >= 1
    assert kb.get_task(primary, child).status == "ready"
    assert len(kb.list_comments(primary, repair)) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_frozen_repair_card_does_not_abort_readiness_and_stays_untouched(detached, kind):
    primary = detached
    child, repair = blocked_child(primary)
    neighbour = kb.create_task(primary, title="independent neighbour")
    with dbc.write_txn(primary):
        primary.execute("UPDATE tasks SET status='todo' WHERE id=?", (neighbour,))
    freeze(primary, repair, kind)
    parent_arrives(primary)
    before = snapshot(primary, repair)
    kb.recompute_ready(primary)  # expected: no IntegrityError; the cleared block is committed
    assert kb.get_task(primary, child).status == "ready"
    assert kb.get_task(primary, neighbour).status == "ready"
    assert snapshot(primary, repair) == before  # strict fence: the frozen repair card gets no note
    unblocked = primary.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='unblocked'", (child,))
    assert [json.loads(r[0])["repair_task_id"] for r in unblocked] == [repair]


# ── finding 2: allowance sweep ───────────────────────────────────────────────────────────────────────────────

def ready_card(conn, title):
    tid, uid = fixtures._released(conn)
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready',title=? WHERE id=?", (title, tid))
    return tid, uid


def hub(monkeypatch, conn, replies):
    calls = []

    class Hub:
        def __init__(self, ident, *, deadline):
            pass

        def reviewer_allowance(self, board, task):
            calls.append(task)
            return replies[task]

        def close(self):
            pass

    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="test-home"))
    monkeypatch.setattr(client, "Hub", Hub)
    return calls


def two_cards(primary, monkeypatch, frozen_first=True):
    a, ua = ready_card(primary, "frozen candidate")
    b, ub = ready_card(primary, "healthy neighbour")
    first, second = sorted([a, b])
    frozen, healthy = (first, second) if frozen_first else (second, first)
    uids = {a: ua, b: ub}
    calls = hub(monkeypatch, primary, {t: reads._reply(primary, t, uids[t]) for t in (a, b)})
    return frozen, healthy, calls


@pytest.mark.parametrize("kind", KINDS)
def test_sweep_skips_frozen_candidate_and_triages_healthy_neighbour(primary, monkeypatch, kind):
    frozen, healthy, calls = two_cards(primary, monkeypatch)
    freeze(primary, frozen, kind)
    before = snapshot(primary, frozen)
    assert maintenance.sweep(primary) == [healthy]  # expected: no IntegrityError, neighbour not starved
    assert kb.get_task(primary, healthy).status == "triage"
    assert kb.get_task(primary, frozen).status == "ready"
    assert frozen not in calls  # no Board read for a frozen card
    assert snapshot(primary, frozen) == before


@pytest.mark.parametrize("kind", KINDS)
def test_frozen_first_card_does_not_starve_a_full_page_of_healthy_cards(primary, monkeypatch, kind):
    cards = dict(ready_card(primary, f"card {i}") for i in range(maintenance.BATCH + 1))
    frozen, *healthy = sorted(cards)
    calls = hub(monkeypatch, primary, {t: reads._reply(primary, t, u) for t, u in cards.items()})
    freeze(primary, frozen, kind)
    before = snapshot(primary, frozen)
    triaged = []
    for _ in range(2):  # page 1 = frozen + 7 healthy, page 2 = the last healthy card
        triaged += maintenance.sweep(primary)
        for k, (after, _next) in list(maintenance._CURSORS.items()):  # cadence is pacing, not under test
            maintenance._CURSORS[k] = (after, 0.0)
    assert sorted(set(triaged)) == healthy  # every healthy card reached; none starved behind the frozen one
    assert frozen not in calls
    assert snapshot(primary, frozen) == before


@pytest.mark.parametrize("kind", KINDS)
def test_dispatch_pass_still_starts_spend_reviews_with_frozen_candidate(primary, monkeypatch, kind):
    from hermes_cli import kanban_dispatch_maintenance as dispatch
    from hermes_cli import kanban_spend_reviewer as reviewer
    frozen, healthy, calls = two_cards(primary, monkeypatch)
    freeze(primary, frozen, kind)
    started = []
    monkeypatch.setattr(reviewer, "start_due", lambda conn: started.append(1))
    dispatch._start_spend_reviews(primary)
    assert started == [1]  # the sweep failure must not suppress start_due
    assert frozen not in calls
    assert kb.get_task(primary, healthy).status == "triage"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seam", ["read", "writer"])
def test_custody_arriving_after_selection_skips_hold_and_continues(primary, monkeypatch, kind, seam):
    from contextlib import contextmanager
    frozen, healthy, calls = two_cards(primary, monkeypatch)
    before = []
    original_read = maintenance.observation.read
    original_writer = kb.write_txn
    armed = [False]

    def take_custody():
        freeze(primary, frozen, kind)
        before.append(snapshot(primary, frozen))
        armed[0] = False

    def read(conn, task_id, **kwargs):
        result = original_read(conn, task_id, **kwargs)
        if task_id == frozen:
            if seam == "read":
                take_custody()
            else:
                armed[0] = True
        return result

    @contextmanager
    def writer(conn, *args, **kwargs):
        if seam == "writer" and armed[0]:
            take_custody()
        with original_writer(conn, *args, **kwargs):
            yield

    monkeypatch.setattr(maintenance.observation, "read", read)
    monkeypatch.setattr(kb, "write_txn", writer)
    monkeypatch.setattr(maintenance.observation, "stuck", lambda *a, **k: pytest.fail("second read after custody"))
    assert maintenance.sweep(primary) == [healthy]
    assert calls == [frozen, healthy]
    assert snapshot(primary, frozen) == before[0]
    assert kb.get_task(primary, healthy).status == "triage"


@pytest.mark.parametrize("kind", ["prepared", "pending"])
def test_sweep_revisits_card_after_authenticated_custody_resolution(primary, monkeypatch, kind):
    task, uid = ready_card(primary, "eventual recovery")
    freeze(primary, task, kind)
    before = snapshot(primary, task)
    calls = hub(monkeypatch, primary, {task: reads._reply(primary, task, uid)})
    assert maintenance.sweep(primary) == []
    assert calls == [] and snapshot(primary, task) == before
    if kind == "prepared":
        with dbc.write_txn(primary):
            transfer.abort_source(primary, dict(task=task, source="worker-a3", destination="hub",
                nonce="repair-test-nonce-0001", expected_version=7, phase="aborted", rows=[]))
    else:
        feed_support.feed(primary, task, version=9, nonce=NONCE)
        with dbc.write_txn(primary):
            primary.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
    assert not transfer.frozen(primary, task)
    maintenance._CURSORS.clear()
    assert maintenance.sweep(primary) == [task]
    assert calls == [task]
    assert kb.get_task(primary, task).status == "triage"


@pytest.mark.parametrize("kind", ["prepared", "pending"])
def test_repair_notes_resume_after_authenticated_custody_resolution(detached, kind):
    from hermes_cli.kanban_db_graph import _tell_repair_card
    child, repair = blocked_child(detached)
    freeze(detached, repair, kind)
    parent_arrives(detached)
    kb.recompute_ready(detached)
    assert kb.get_task(detached, child).status == "ready"
    assert kb.list_comments(detached, repair) == []
    if kind == "prepared":
        with dbc.write_txn(detached):
            transfer.abort_source(detached, dict(task=repair, source="worker-a3", destination="hub",
                nonce="repair-test-nonce-0001", expected_version=7, phase="aborted", rows=[]))
    else:
        feed_support.feed(detached, repair, version=9, nonce=NONCE)
    with dbc.write_txn(detached):
        _tell_repair_card(detached, repair, child, 1, "parent_found")
    assert len(kb.list_comments(detached, repair)) == 1
