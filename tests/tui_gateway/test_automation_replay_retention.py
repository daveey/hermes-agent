"""Terminal payload compaction preserves exact dedupe and pending custody."""
import sqlite3

import pytest

from tests.tui_gateway import test_automation_replay_handoff as helpers

journal = helpers.journal
wired = helpers.wired


def test_compaction_dedupes_arbitrarily_late_publication_but_accepts_old_first_publish(journal):
    original = helpers.item("old", due=0, created=1)
    journal.publish("chat", [original])
    batch = helpers.take(journal)
    journal.commit_message(batch, helpers.writer)
    assert journal.settle(batch)
    journal.compact()
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT count(*) FROM automation_replay_items").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM automation_replay_batches").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM automation_replay_tombstones").fetchone()[0] == 1
    journal.publish("chat", [original])
    assert helpers.take(journal, now=1e30) is None
    with pytest.raises(ValueError, match="identity"):
        journal.publish("chat", [{**original, "text": "conflict"}])
    with pytest.raises(ValueError, match="identity"):
        journal.publish("foreign", [original])
    journal.publish("chat", [helpers.item("never-published", due=0, created=1)])
    assert helpers.take(journal, now=1e30) is not None


def test_compaction_never_prunes_pending_future_or_committed_work(journal):
    journal.publish("future", [helpers.item("future", due=1e30, created=1)])
    journal.publish("chat", [helpers.item("committed", due=0, created=1)])
    pending = helpers.take(journal)
    receipt = journal.commit_message(pending, helpers.writer)
    for _ in range(3):
        journal.compact(limit=1)
    recovered = helpers.take(journal, now=1e20)
    assert recovered["message_row_id"] == receipt["message_row_id"]
    assert journal.claim("future", 1e20, "successor", lambda _: False) is None
    assert journal.claim("future", 1e30, "successor", lambda _: False) is not None


def test_partial_stop_compaction_preserves_survivor_and_fences_old_claim(journal):
    covered = helpers.item("covered", due=0, created=10)
    survivor = helpers.item("survivor", due=0, created=30, persisted=False)
    journal.publish("chat", [covered, survivor])
    old = helpers.take(journal)
    journal.cancel("chat", 20, stop=True)
    journal.compact()
    journal.publish("chat", [covered, survivor])
    assert journal.commit_message(old, helpers.writer) is None
    batch = helpers.take(journal)
    import json
    assert [i["id"] for i in json.loads(batch["payload"])] == ["survivor"]


def test_compaction_bounds_rows_and_rollback_keeps_payload_and_identity(journal):
    for name in ("one", "two", "three"):
        journal.publish(name, [helpers.item(name, due=0)])
        batch = journal.claim(name, 100, "dead", lambda _: False)
        assert journal.discard(batch)
    with sqlite3.connect(journal.path) as conn:
        conn.execute("CREATE TRIGGER fail_compaction BEFORE DELETE ON automation_replay_items "
                     "BEGIN SELECT RAISE(ABORT,'compaction interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
        journal.compact(limit=1)
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT count(*) FROM automation_replay_items").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM automation_replay_tombstones").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_compaction")
    journal.compact(limit=1)
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT count(*) FROM automation_replay_items").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM automation_replay_batches").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM automation_replay_tombstones").fetchone()[0] == 1


def test_idle_compaction_does_not_take_write_transaction(journal, monkeypatch):
    journal.publish("chat", [helpers.item("pending", due=1e30)])
    def unexpected():
        pytest.fail("idle compaction must not acquire the writer")
    monkeypatch.setattr(journal, "transaction", unexpected)
    journal.compact()


def test_consumer_compaction_failure_does_not_block_dispatch(wired, monkeypatch):
    from tui_gateway import automation_replay as ar
    from tui_gateway.automation_replay_handoff import Journal
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    wired.publish(helpers.KEY, [helpers.item(due=0)])
    def fail(*a):
        raise sqlite3.OperationalError("compaction unavailable")
    monkeypatch.setattr(Journal, "compact", fail)
    calls = []
    assert ar.consume_handoffs([wired.path.parent], now=100,
                               wake=lambda *a, **kw: calls.append(1) or "missing") == 1
    assert calls == [1] and wired.owners() == []


def test_child_publication_cannot_cancel_foreign_parent_and_rolls_back_on_conflict(journal):
    journal.publish("foreign", [helpers.item("foreign", due=0)])
    parent = journal.claim("foreign", 100, "owner", lambda _: False)
    child = {**helpers.item("child", due=0), "source_batch": parent["id"]}
    journal.publish("chat", [child])
    assert journal.release(parent)
    journal.publish("chat", [helpers.item("conflict", due=0)])
    same = helpers.take(journal)
    with pytest.raises(ValueError, match="identity"):
        journal.publish("chat", [{**helpers.item("new", due=0), "source_batch": same["id"]},
                                 {**helpers.item("conflict", due=0), "text": "different"}])
    assert journal.release(same)
