"""SessionState v2 store: recompute, digest gate, rev persistence, timers, reconciliation. [daveey]"""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_session_state_schema import validate
from hermes_session_state_store import SessionStateStore
from tests.session_state.conftest import HUMAN_REQUEST, drain


def test_recompute_publishes_a_valid_object_for_a_linked_human_ask(store, board):
    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    obj = store.recompute("chat")
    assert validate(obj) == []
    card = obj["cards"][f"default/{task}"]
    assert (card["lifecycle"], card["blocked_on"], card["red"]) == ("blocked", "human", True)
    assert obj["header"]["status"] == "needs_you" and obj["header"]["colour"] == "red"
    assert obj["header"]["pips"] == [{"section": "needs_you", "count": 1}]
    assert [a["card"] for a in obj["asks"]] == [f"default/{task}"]
    assert store.pushed[-1] == obj and obj["rev"] == 1


def test_identical_recompute_does_not_churn_rev_or_push(store, board, clock):
    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    first = store.recompute("chat")
    clock.now += 5
    again = store.recompute("chat")
    assert again["rev"] == first["rev"] and len(store.pushed) == 1
    assert store.metrics()["unchanged"] == 1 and store.current_rev() == first["rev"]


def test_identical_recompute_writes_nothing_to_state_db(store, board, db, clock):
    """Digest no-churn is write-free: a no-op recompute leaves state.db's commit counter where it was."""
    import sqlite3
    from contextlib import closing

    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    store.recompute("chat")
    with closing(sqlite3.connect(str(db.db_path))) as observer:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        clock.now += 5
        store.recompute("chat")
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before
        db.create_session("other", source="desktop")  # positive control: another connection's commit moves it
        assert observer.execute("PRAGMA data_version").fetchone()[0] != before


def test_rev_is_monotonic_across_a_store_restart(store, board, db, clock):
    task = kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    first = store.recompute("chat")
    store.stop()
    reopened = SessionStateStore(db, clock=clock)
    assert reopened.current_rev() == first["rev"]
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    later = reopened.recompute("chat")
    assert later["rev"] > first["rev"] and reopened.get("chat") == later


def test_rev_is_store_wide_across_sessions(store, board, db):
    db.create_session("other", source="desktop")
    kb.create_task(board, title="A", assignee="worker", session_id="chat")
    kb.create_task(board, title="B", assignee="worker", session_id="other")
    a = store.recompute("chat")
    b = store.recompute("other")
    assert (a["rev"], b["rev"]) == (1, 2)
    assert [o["identity"]["lineage"] for o in store.changes_since(1)] == ["other"]


def test_dependency_closure_parents_are_indexed_and_shown(store, board):
    parent = kb.create_task(board, title="Build", assignee="worker")
    child = kb.create_task(board, title="Ship", assignee="worker", session_id="chat", parents=[parent])
    obj = store.recompute("chat")
    assert obj["cards"][f"default/{child}"]["blocked_on"] == "card"
    assert obj["cards"][f"default/{child}"]["parents"] == [f"default/{parent}"]
    assert obj["cards"][f"default/{parent}"]["linked"] is False
    assert store.lineages_for_card(f"default/{parent}") == ["chat"]
    assert obj["header"]["counts"]["waiting"] == 1  # closure-only cards are shown, not counted


def test_unknown_session_is_none_and_not_persisted(store):
    assert store.recompute("nope") is None
    assert store.get("nope") is None


def test_divergence_against_the_stored_v1_status_is_logged(store, board, db, tmp_path):
    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET outcome_json = ? WHERE id = 'chat'",
        (json.dumps({"kind": "done", "summary": "x", "status": {"kind": "done", "status_version": 0}}),)))
    store.recompute("chat")
    lines = (tmp_path / "logs" / "divergence.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["v1"]["kind"] == "done" and store.metrics()["divergences"] == 1


def test_reconciliation_counts_a_missed_event_as_a_diff(store, board):
    task = kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    store.recompute("chat")
    assert store.reconcile() == 0
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)  # no event delivered
    assert store.reconcile() == 1
    assert store.metrics()["reconcile_diffs"] == 1 and store.get("chat")["header"]["status"] == "needs_you"


def test_reconciliation_runs_from_the_worker_every_interval(store, board, clock):
    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    store.recompute("chat")
    clock.now += 301
    drain(store, rounds=1)
    assert store.metrics()["reconcile_runs"] == 1
