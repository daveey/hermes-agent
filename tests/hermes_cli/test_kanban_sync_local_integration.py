"""Production sync-round composition keeps local recovery while idle rounds skip remote-parent work.

These drive ``ksc.sync_board`` (the real ``_sync_board_locked`` primary path), not the standalone gate: a local
clobber that no pull causes must still be healed by the round itself (design C13), and a settled board must do no
remote-parent mirror work until the 30 s floor (which also repairs a local mirror-table clobber).
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_parent_refresh as gate
from tests.hermes_cli import test_kanban_move_parent_evidence as helpers
from tests.hermes_cli import test_kanban_sync_terminal_regress as base

kanban_home, svc, live = base.kanban_home, base.svc, base.live
moved_parent = helpers.moved_parent
gws = helpers.gws
ME, OTHER, BOARD = base.ME, base.OTHER, base.BOARD
PLACEHOLDER = "card left running (done)"  # kanban_sync_divergence._LEASE_END_PLACEHOLDER


def _uncaptured(live, sql, params=()):
    """A local clobber no pull and no capture trigger accounts for."""
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute(sql, params)
        live.execute("UPDATE kanban_sync_state SET applying = 0")


@pytest.fixture
def board_input(monkeypatch):
    """Record what each round's pull read from the Board and what it applied to the replica.

    ``RoundResult.pulled`` is a ``Pulled`` (an ``int``, default 0), so ``is not None`` holds even for a pull that
    applied a heal. The pull contract is: count of feed changes read, ``more``/``busy`` flags, ``status_changed``
    (every status the pull applied), and the replica cursor it advanced; an idle poll applies nothing. Only applies
    made inside a pull count: the round's own heal applies replica rows to the live file through the same function."""
    pulls, applied, inside = [], [], []
    real_pull, real_apply = ksc.pull, sync.apply_remote_changes

    def pull(conn, *a, **k):
        before = (sync.sync_state(conn) or {}).get("cursor")
        inside.append(True)
        try:
            out = real_pull(conn, *a, **k)
        finally:
            inside.pop()
        pulls.append({"n": int(out), "more": out.more, "busy": out.busy, "status_changed": dict(out.status_changed),
                      "cursor": (before, (sync.sync_state(conn) or {}).get("cursor"))})
        return out

    def apply(conn, changes, *a, **k):
        if inside:
            applied.append(list(changes))
        return real_apply(conn, changes, *a, **k)

    monkeypatch.setattr(ksc, "pull", pull)
    monkeypatch.setattr(sync, "apply_remote_changes", apply)
    return pulls, applied


def _assert_no_board_input(result, pulls, applied):
    assert int(result.pulled) == 0 and not result.pulled.more and not result.pulled.busy
    assert result.pulled.status_changed == {}
    assert pulls, "the round must have polled the Board"  # a round may poll more than once; every poll must be idle
    for one in pulls:
        assert one["n"] == 0 and one["status_changed"] == {} and one["cursor"][0] == one["cursor"][1], one
        assert not one["more"] and not one["busy"], one
    assert not any(applied), f"the round's pull applied Board changes: {applied}"


def _settled_completed_card(live, kanban_home, svc):
    tid, run_uid = base._running_card(live, kanban_home, svc)
    run_id = live.execute("SELECT id FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0]
    with kbc.write_txn(live):
        kb._append_event(live, tid, "completed", {"summary": "done"}, run_id=run_id)
    base._worker_ends(svc, tid, run_uid, status="done", outcome="completed", completed_at=int(time.time()))
    for _ in range(4):  # pull the end, then idle: nothing left to move
        base._round(kanban_home, svc)
    row = live.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row[0], row[1]) == ("done", None)
    return tid, run_id


def test_running_over_ended_run_clobber_healed_by_round_without_pull(live, kanban_home, svc, board_input):
    tid, run_id = _settled_completed_card(live, kanban_home, svc)
    pulls, applied = board_input
    pulls.clear()
    applied.clear()
    _uncaptured(live, "UPDATE tasks SET status = 'running', completed_at = NULL, current_run_id = ? WHERE id = ?",
                (run_id, tid))
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"
    result = base._round(kanban_home, svc)  # the Board has nothing new: only the round's own heal can repair it
    _assert_no_board_input(result, pulls, applied)
    row = live.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row[0], row[1]) == ("done", None)
    assert live.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'status_healed'",
                        (tid,)).fetchone()[0] == 1, "the heal sweep itself must have run"


def test_placeholder_run_end_clobber_healed_by_round_without_pull(live, kanban_home, svc, board_input):
    _tid, run_id = _settled_completed_card(live, kanban_home, svc)
    pulls, applied = board_input
    pulls.clear()
    applied.clear()
    _uncaptured(live, "UPDATE task_runs SET status = 'released', outcome = 'released', error = ?, summary = NULL "
                      "WHERE id = ?", (PLACEHOLDER, run_id))
    result = base._round(kanban_home, svc)
    _assert_no_board_input(result, pulls, applied)
    row = live.execute("SELECT status, outcome, error FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert tuple(row) == ("done", "completed", None)


@pytest.fixture
def mirrored(moved_parent, monkeypatch):
    hub, worker, task, parent = moved_parent
    gate.reset()
    now = [1000.0]
    monkeypatch.setattr(gate, "_clock", lambda: now[0])
    calls = []
    original = sync.refresh_remote_parents

    def spy(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(sync, "refresh_remote_parents", spy)
    # The production sync loop runs every round inside one connection scope; the gate keys on the kept handles.
    with ksc.connection_scope():
        for _ in range(3):
            worker.round()  # settle: the gate's first pass is the full path
        calls.clear()
        yield hub, worker, task, parent, now, calls
    gate.reset()


FLOOR_S = 30.0  # literal: the stated floor, not the constant under test (as the links pass's 300 s below)


def test_unchanged_board_rounds_do_no_remote_parent_work(mirrored):
    _hub, worker, _task, _parent, now, calls = mirrored
    base_t = now[0]  # the settle rounds' full pass stamped this time
    for _ in range(4):
        now[0] += 1.0
        worker.round()
    assert calls == [], "an unchanged board's rounds must not re-run the remote-parent mirror"
    now[0] = base_t + FLOOR_S - 0.001
    worker.round()
    assert calls == [], "re-verified before the stated 30 s floor"
    now[0] = base_t + FLOOR_S
    worker.round()
    assert calls == [True], "the stated 30 s floor must still re-verify"


def test_local_mirror_clobber_recovers_at_floor_through_real_rounds(mirrored):
    _hub, worker, task, parent, now, calls = mirrored
    base_t = now[0]
    assert helpers.evidence.result(worker.conn, task, parent)
    worker.conn.execute("DELETE FROM kanban_move_parent_results")
    now[0] += 1.0
    worker.round()
    assert calls == [] and helpers.evidence.result(worker.conn, task, parent) is None
    now[0] = base_t + FLOOR_S - 0.001
    worker.round()
    assert calls == [] and helpers.evidence.result(worker.conn, task, parent) is None
    now[0] = base_t + FLOOR_S
    worker.round()
    assert calls == [True]
    assert helpers.evidence.result(worker.conn, task, parent)


@pytest.mark.parametrize("table", ["tasks", "task_runs", "task_events"])
def test_pulled_parent_rows_refresh_only_changed_parent(mirrored, monkeypatch, table):
    hub, worker, _task, parent, now, full_calls = mirrored
    row_calls = []
    real = gate._refresh_rows

    def rows(live, replica, targets, scoped):
        row_calls.append(frozenset(targets))
        return real(live, replica, targets, scoped)

    monkeypatch.setattr(gate, "_refresh_rows", rows)
    hub.enter()
    with kbc.write_txn(hub.conn):
        if table == "tasks":
            hub.conn.execute("UPDATE tasks SET title='parent edited' WHERE id=?", (parent,))
        elif table == "task_runs":
            hub.conn.execute("UPDATE task_runs SET summary='new review evidence' WHERE task_id=?", (parent,))
        else:
            hub.conn.execute("UPDATE task_events SET kind='note' WHERE task_id=? AND kind='claimed'", (parent,))
    hub.round()  # publish through the real fake-Board transport, not a direct gate call
    full_calls.clear()  # the source gateway's first refresh is not the target's keyed pull
    row_calls.clear()
    now[0] += 1
    result = worker.round()
    assert int(result.pulled) > 0
    assert row_calls and parent in set().union(*row_calls), "the pull-applied parent must take the keyed path"
    assert full_calls == [], "changed rows must not enumerate every foreign parent"
    payload = json.loads(helpers.evidence.stored(worker.conn, [parent])[parent])
    if table == "task_runs":
        assert payload["summary"] == "new review evidence"
    elif table == "task_events":
        assert not payload["was_claimed"]
    row_calls.clear()
    worker.round()
    assert row_calls == [] and full_calls == [], "settled follow-up must skip both mirror paths"


def test_links_pass_keeps_its_300s_floor(live, kanban_home, monkeypatch):
    from hermes_cli import kanban_chat_links
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli.kanban_creator_empty import ensure
    sync.set_mode(live, "primary")
    # Test the settled deadline, not the initial backfill schema invalidation.
    assert ensure(live)
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    ksc._LINKS_DONE.clear()
    now = [5000.0]
    monkeypatch.setattr(ksc, "_links_clock", lambda: now[0])
    calls = []
    real = kanban_chat_links.reconcile
    monkeypatch.setattr(kanban_chat_links, "reconcile", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    boards = [(BOARD, kanban_home / "kanban.db")]
    for _ in range(3):
        ksc.reconcile_links(boards, ME, skip_unchanged=True)
    assert len(calls) == 1
    now[0] += 299.0  # literal: the stated floor, not the constant under test
    ksc.reconcile_links(boards, ME, skip_unchanged=True)
    assert len(calls) == 1
    now[0] += 1
    ksc.reconcile_links(boards, ME, skip_unchanged=True)
    assert len(calls) == 2
