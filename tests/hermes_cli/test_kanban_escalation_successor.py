"""Re-park owner reuse across terminal episodes, transactions and concurrent retries."""
import concurrent.futures
import json
from pathlib import Path
import threading

import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_awaiting as helpers

kanban_home = helpers.kanban_home
RECORD = {"kind": "external", "ref": "vendor-4711", "why": "pending", "premise": "fixed-premise"}


def _escalate(conn, tid, reason, record=None):
    with kb.write_txn(conn):
        return kaw.escalate(conn, tid, record=record or RECORD, reason=reason)


def _finish(conn, owner):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (owner,))


def test_open_successor_is_reused_after_original_owner_completed(kanban_home, monkeypatch):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        first = _escalate(conn, tid, "initial incident")
        _finish(conn, first)
        successor = _escalate(conn, tid, "new incident evidence A")
        assert successor != first
        repeated = _escalate(conn, tid, "same incident evidence B")
        assert repeated == successor
        assert kb.get_task(conn, first).status == "done"
        assert kb.get_task(conn, successor).idempotency_key.endswith(f":after:{first}")
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == kaw.ESCALATED_EVENT]
        assert [e['reason'] for e in events] == ["initial incident", "new incident evidence A", "same incident evidence B"]


def test_keyless_legacy_successor_is_adopted_without_title_matching(kanban_home, monkeypatch):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        first = _escalate(conn, tid, "initial")
        _finish(conn, first)
        successor = _escalate(conn, tid, "successor")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET idempotency_key=NULL, title='renamed investigation' WHERE id=?", (successor,))
        assert _escalate(conn, tid, "retry") == successor


def test_distinct_target_and_premise_create_distinct_owners(kanban_home, monkeypatch):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        a, b = helpers._running(conn), helpers._running(conn)
        owner_a = _escalate(conn, a, "A")
        owner_b = _escalate(conn, b, "B")
        owner_c = _escalate(conn, a, "C", {**RECORD, "premise": "different"})
        assert len({owner_a, owner_b, owner_c}) == 3
        assert _escalate(conn, a, "A retry") == owner_a


@pytest.mark.parametrize("terminal", ["done", "archived"])
def test_terminal_latest_episode_is_not_reopened_or_skipped(kanban_home, monkeypatch, terminal):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        first = _escalate(conn, tid, "initial")
        _finish(conn, first)
        second = _escalate(conn, tid, "second")
        with kb.write_txn(conn):
            # A stale older OPEN row must not beat the latest terminal episode.
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (first,))
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (terminal, second))
        third = _escalate(conn, tid, "third")
        assert third not in (first, second)
        assert kb.get_task(conn, second).status == terminal
        assert _escalate(conn, tid, "third retry") == third


@pytest.mark.parametrize("fault", ["create_task", "_link", "_append_event"])
def test_crash_window_rolls_back_owner_and_evidence(kanban_home, monkeypatch, fault):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        first = _escalate(conn, tid, "first")
        _finish(conn, first)
        counts = [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("tasks", "task_events", "task_links")]
        original = getattr(kb, fault)

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected crash after write")

        with monkeypatch.context() as patch:
            patch.setattr(kb, fault, crash)
            with pytest.raises(RuntimeError, match="injected crash"):
                _escalate(conn, tid, "uncommitted evidence")
        assert counts == [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                          for table in ("tasks", "task_events", "task_links")]
        successor = _escalate(conn, tid, "recovered evidence")
        assert _escalate(conn, tid, "retry evidence") == successor


def test_concurrent_retry_after_terminal_episode_retains_all_nine_reasons(kanban_home, monkeypatch):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        first = _escalate(conn, tid, "first")
        _finish(conn, first)
    barrier = threading.Barrier(2)

    def submit(n):
        with kbc.connect_closing() as conn:
            barrier.wait(timeout=10)
            return _escalate(conn, tid, f"evidence {n}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(pool.map(submit, range(2)))
    with kbc.connect_closing() as conn:
        owners.extend(_escalate(conn, tid, f"evidence {n}") for n in range(2, 9))
        assert len(set(owners)) == 1
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == kaw.ESCALATED_EVENT]
        assert {e['reason'] for e in events[1:]} == {f"evidence {n}" for n in range(9)}
        assert {e['owner_task_id'] for e in events[1:]} == {owners[0]}
        assert conn.execute("SELECT count(*) FROM tasks WHERE status NOT IN ('done','archived') AND id != ?", (tid,)).fetchone()[0] == 1


@pytest.mark.parametrize("finish_each", [False, True])
def test_frozen_nine_records_preserve_open_vs_completed_episode_boundary(kanban_home, monkeypatch, finish_each):
    # Frozen E bundle SHA256 5976fc63e99f74cc3db9ff42c3f325b27ad8ffc5f09bedce8fbfb6646488da64
    # and read-only canonical source escalation rows. E's nine owners are all DONE and
    # name different creator dispositions: the open-only variant is a counterfactual,
    # NOT proof that eight historical incidents could have been suppressed.
    rows = json.loads((Path(__file__).parent / "fixtures/repark-nine-records.json").read_text())
    assert len(rows) == 9
    assert len({row['record']['why'] for row in rows}) == 9
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        owners = []
        for row in rows:
            owners.append(_escalate(conn, tid, row['record']['why'], row['record']))
            if finish_each:
                _finish(conn, owners[-1])
        assert len(set(owners)) == (9 if finish_each else 1)
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == kaw.ESCALATED_EVENT]
        assert [e['reason'] for e in events] == [row['record']['why'] for row in rows]


def test_schedule_entrypoint_reuses_open_successor(kanban_home, monkeypatch):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    args = {"reason": "Waiting for vendor ticket 4711", "wake_check": "exit 1",
            "awaiting": {"kind": "external", "ref": "vendor ticket 4711", "why": "response pending"}}
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        for n in range(2):
            helpers._as_worker(monkeypatch, conn, tid)
            assert helpers._tool("schedule", args).get("status") == "scheduled"
            assert kb.unblock_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            assert kb.claim_task(conn, tid, claimer="worker")
        record = kaw.resolve(conn, tid, awaiting=args['awaiting'], reason=args['reason'], wake_check=args['wake_check'])
        record['premise'] = kaw.premise_hash(record, args['reason'], args['wake_check'])
        # Seed an older terminal episode without resetting the current two-park streak.
        with kb.write_txn(conn):
            first = kb.create_task(conn, title="old owner", assignee="worker", idempotency_key=f"{kaw.ESCALATION_PREFIX}{tid}:{record['premise']}")
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (first,))
        owners = []
        for attempt in range(2):
            if attempt:
                # Replay another judged re-park streak without deleting its prior owner evidence.
                with kb.write_txn(conn):
                    for _ in range(2):
                        kb._append_event(conn, tid, "scheduled", {"awaiting": record})
            assert kb.schedule_task(conn, tid, **args)
            owners.append(kaw.escalated_owner(conn, tid))
        assert owners[0] is not None and owners[0] == owners[1]
