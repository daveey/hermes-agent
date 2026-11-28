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


def _rows(db):
    with db._read_ctx() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            "session_state", "session_state_cards", "session_state_alias", "session_state_resolutions")}


def test_reconcile_prunes_gone_and_long_ended_sessions(db, board, clock, tmp_path):
    from hermes_session_state_store import SessionStateStore

    made = SessionStateStore(db, clock=clock, retain_s=3600)
    for sid in ("old", "recent", "gone"):
        db.create_session(sid, source="desktop")
        kb.create_task(board, title=sid, assignee="worker", session_id=sid)
        assert made.recompute(sid) is not None
        made.add_resolution(sid, {"request_id": f"r-{sid}", "card": "x", "episode": None})
    made.recompute("chat")
    db._execute_write(lambda c: c.execute("UPDATE sessions SET ended_at=? WHERE id='old'", (clock.now - 7200,)))
    db._execute_write(lambda c: c.execute("UPDATE sessions SET ended_at=? WHERE id='recent'", (clock.now - 60,)))
    db._execute_write(lambda c: c.execute("DELETE FROM sessions WHERE id='gone'"))
    made.reconcile()  # 'recent' ending changes its object: a legitimate diff, not part of this assertion
    assert made.metrics()["pruned"] == 2
    assert made.get("old") is None and made.get("gone") is None
    assert made.get("recent") is not None and made.get("chat") is not None
    assert made.resolutions("old") == [] and [r["request_id"] for r in made.resolutions("recent")] == ["r-recent"]
    with db._read_ctx() as conn:
        left = {r[0] for r in conn.execute("SELECT DISTINCT lineage FROM session_state_cards")}
        aliases = {r[0] for r in conn.execute("SELECT DISTINCT lineage FROM session_state_alias")}
    assert left <= {"recent", "chat"} and aliases <= {"recent", "chat"}
    counts = _rows(db)
    assert made.reconcile() == 0 and made.metrics()["pruned"] == 2 and _rows(db) == counts  # nothing more


def test_one_sweep_recomputes_a_bounded_round_robin_batch(db, board, clock):
    from hermes_session_state_store import SessionStateStore

    made = SessionStateStore(db, clock=clock, reconcile_batch=2)
    lineages = ["chat", "s1", "s2", "s3", "s4"]
    for sid in lineages[1:]:
        db.create_session(sid, source="desktop")
    for sid in lineages:
        made.recompute(sid)
    swept: list[str] = []
    real = made.recompute

    def spy(session_id, *, trigger="manual"):
        if trigger == "reconcile":
            swept.append(session_id)
        return real(session_id, trigger=trigger)
    made.recompute = spy  # type: ignore[method-assign]
    for _ in range(3):
        before = len(swept)
        made.reconcile()
        assert len(swept) - before <= 2  # bounded however many rows are persisted
    assert sorted(swept[:5]) == sorted(lineages) and len(swept) == 6  # all covered in ceil(5/2) sweeps, then wraps
    assert swept[5] == swept[0]


def test_live_events_are_served_between_sweep_items(db, board, clock):
    from hermes_session_state_store import SessionStateStore

    made = SessionStateStore(db, clock=clock)
    for sid in ("s1", "s2"):
        db.create_session(sid, source="desktop")
    for sid in ("chat", "s1", "s2"):
        made.recompute(sid)
    order: list[tuple[str, str]] = []
    real = made.recompute

    def spy(session_id, *, trigger="manual"):
        order.append((session_id, trigger))
        if trigger == "reconcile" and len(order) == 1:
            made.emit_session("s2", "card_change")  # a live event arrives during the sweep
        return real(session_id, trigger=trigger)
    made.recompute = spy  # type: ignore[method-assign]
    made.reconcile()
    assert order[:2] == [("chat", "reconcile"), ("s2", "card_change")]  # not queued behind the whole sweep
