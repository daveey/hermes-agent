"""Durable retirement custody; deterministic crash and concurrent-publisher boundaries."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tui_gateway.automation_replay_handoff import Journal


@pytest.fixture
def journal(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE transcript(id INTEGER PRIMARY KEY, uid TEXT UNIQUE, content TEXT)")
    return Journal(home)


def item(identity="a", *, due=100, created=50, persisted=True, kind="kanban_notice"):
    return {"id": identity, "due": due, "created_at": created, "persisted": persisted,
            "text": identity, "display_kind": kind, "display_metadata": {"task_ids": ["t_0123456789abcdef"]},
            "cause": "quota", "failed_at": 40, "attempt": 1, "busy_rechecks": 2}


def take(journal, now=100, holder="process-birth-A", alive=lambda _: False):
    return journal.claim("chat", now, holder, alive)


def writer(conn, uid):
    cursor = conn.execute("INSERT INTO transcript(uid,content) VALUES(?,?)", (uid, "mixed replay"))
    return cursor.lastrowid, "chat"


def test_mixed_payload_preserves_later_reset_and_original_fields(journal):
    items = [item(kind="process_complete"), item("b", due=3600, persisted=False)]
    journal.publish("chat", items)
    journal.publish("chat", items)  # retry after durable acceptance, before predecessor drops custody
    assert take(journal, 3599) is None
    batch = take(journal, 3600)
    assert json.loads(batch["payload"]) == items
    assert take(journal, 100000, holder="successor", alive=lambda _: True) is None
    journal.commit_message(batch, writer)
    assert journal.settle(batch)
    assert not journal.owners()
    journal.publish("chat", items)
    assert take(journal, 100000) is None  # replaying old publication never resurrects settled IDs


def test_uncommitted_batch_cannot_settle(journal):
    journal.publish("chat", [item()])
    batch = take(journal)
    assert not journal.settle(batch)
    assert journal.owners() == ["chat"]


def test_conflicting_identity_is_not_silently_dropped(journal):
    journal.publish("chat", [item()])
    with pytest.raises(ValueError, match="identity"):
        journal.publish("other-chat", [item()])
    with pytest.raises(ValueError, match="identity"):
        journal.publish("chat", [item(due=200)])


def test_committed_mixed_batch_remains_one_row_until_context_covers_it(journal):
    journal.publish("chat", [item(), item("unpersisted", persisted=False)])
    batch = take(journal)
    committed = journal.commit_message(batch, writer, now=90)
    journal.cancel("chat", 60)
    assert journal.release(batch)
    survivor = take(journal)
    recovered = journal.commit_message(survivor, writer, now=100)
    assert recovered["message_uid"] == committed["message_uid"]
    journal.cancel("chat", 95)
    assert not journal.settle(survivor)
    assert not journal.owners()
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT count(*) FROM transcript").fetchone()[0] == 1


def test_warm_successor_observes_late_publication(journal):
    assert journal.owners() == []
    journal.publish("chat", [item()])
    assert journal.owners() == ["chat"]
    assert take(journal) is not None


def test_join_during_claim_does_not_mutate_or_get_settled_with_old_batch(journal):
    journal.publish("chat", [item()])
    batch = take(journal)
    journal.publish("chat", [item("b")])
    assert [row["id"] for row in json.loads(batch["payload"])] == ["a"]
    journal.commit_message(batch, writer)
    assert journal.settle(batch)
    successor = take(journal)
    assert [row["id"] for row in json.loads(successor["payload"])] == ["b"]


def test_dead_claim_recovery_keeps_identity_and_fences_stale_settlement(journal):
    journal.publish("chat", [item()])
    abandoned = take(journal)
    successor = take(journal, holder="process-birth-B")
    assert successor["id"] == abandoned["id"]
    assert successor["token"] != abandoned["token"]
    assert not journal.settle(abandoned)
    assert not journal.release(abandoned)
    assert journal.commit_message(abandoned, writer) is None
    journal.commit_message(successor, writer)
    assert journal.settle(successor)


def test_transcript_commit_and_receipt_are_one_transaction(journal):
    journal.publish("chat", [item()])
    batch = take(journal)

    def crash_before_commit(conn, uid):
        writer(conn, uid)
        raise RuntimeError("consumer died before commit")

    with pytest.raises(RuntimeError, match="before commit"):
        journal.commit_message(batch, crash_before_commit)
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT count(*) FROM transcript").fetchone()[0] == 0
    committed = journal.commit_message(batch, writer)
    # Death after COMMIT, before dispatch or settlement: exact existing row is restaged.
    successor = take(journal, holder="process-birth-B")

    def duplicate_writer(*_):
        pytest.fail("must reuse committed row")

    recovered = journal.commit_message(successor, duplicate_writer)
    assert recovered["message_row_id"] == committed["message_row_id"]
    assert recovered["message_uid"] == committed["message_uid"]
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT count(*) FROM transcript").fetchone()[0] == 1


@pytest.mark.parametrize("when", ["before_publish", "after_claim", "after_commit"])
def test_stop_fences_late_publish_claim_and_restart(journal, when):
    batch = None
    if when != "before_publish":
        journal.publish("chat", [item()])
        batch = take(journal)
        if when == "after_commit":
            journal.commit_message(batch, writer)
    journal.cancel("chat", 60, stop=True)
    journal.publish("chat", [item()])
    fresh = Journal(journal.path.parent)
    assert fresh.owners() == [] and take(fresh) is None
    if batch:
        assert not journal.settle(batch)
        assert journal.commit_message(batch, writer) is None
    fresh.publish("chat", [item("new-after-stop", created=61)])
    assert take(fresh) is not None


def test_context_watermark_preserves_unpersisted_and_later_join(journal):
    journal.publish("chat", [item(), item("unpersisted", persisted=False), item("later", created=80)])
    claimed = take(journal)
    journal.cancel("chat", 60)
    assert not journal.settle(claimed)
    survivor = take(journal)
    assert {row["id"] for row in json.loads(survivor["payload"])} == {"unpersisted", "later"}
    # A later publication from the predecessor is governed by the same watermark.
    journal.publish("chat", [item("late-published-old", created=55)])
    journal.commit_message(survivor, writer)
    assert journal.settle(survivor)
    assert take(journal) is None


def test_release_is_not_stop_and_new_instance_recovers(journal):
    journal.publish("chat", [item()])
    batch = take(journal)
    assert journal.release(batch)
    assert take(Journal(journal.path.parent)) is not None


def test_exact_profile_isolation_and_missing_profile_refusal(journal, tmp_path):
    home = tmp_path / "other"
    home.mkdir()
    with pytest.raises(FileNotFoundError):
        Journal(home)
    with sqlite3.connect(home / "state.db"):
        pass
    other = Journal(home)
    journal.publish("chat", [item()])
    other.publish("chat", [item()])
    journal.cancel("chat", 60, stop=True)
    assert take(journal) is None
    assert take(other) is not None


def test_concurrent_consumers_have_one_claim_and_publish_is_idempotent(journal):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: journal.publish("chat", [item()]), range(8)))
        batches = list(pool.map(lambda n: take(journal, holder=f"birth-{n}", alive=lambda _: True), range(4)))
    claimed = [batch for batch in batches if batch is not None]
    assert len(claimed) == 1
    assert len(json.loads(claimed[0]["payload"])) == 1


# --- wiring: retirement -> journal -> successor pass (automation_replay) -----------------------------------------
import threading
import types

from tui_gateway import automation_replay as ar

KEY = "20261119_232638_a67251"


@pytest.fixture
def wired(journal, monkeypatch):
    monkeypatch.setattr(ar, "ENABLED", True)
    monkeypatch.setattr(threading, "Timer", lambda delay, fn: types.SimpleNamespace(
        start=lambda: None, daemon=False, cancel=lambda: None))
    ar.reset()
    yield journal
    ar.reset()


def _arm_mixed(home, now=1000.0):
    ar.schedule(KEY, text="report", display_kind="process_complete", display_metadata={}, due=now + 60,
                cause="quota", persisted=True, now=now, profile_home=home)
    ar.schedule(KEY, text="card done", display_kind="kanban_notice", display_metadata={"task_ids": ["t_0123456789abcdef"]},
                due=now + 60, cause="quota", persisted=True, now=now, profile_home=home)


def test_retired_fire_hands_off_instead_of_spending_the_busy_budget(wired, monkeypatch):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    woken = []
    monkeypatch.setattr("tui_gateway.server._wake_session_turn", lambda *a, **k: woken.append(a) or "busy")
    for _ in range(ar.BUSY_RECHECKS_MAX + 3):  # far past the old busy window: nothing is spent, nothing woken
        ar._fire(KEY)
    assert woken == [] and not ar.pending(KEY)
    assert wired.owners() == [KEY]


def test_successor_replays_handed_off_mixed_delivery_exactly_once_in_exact_profile(wired, monkeypatch):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    ar._fire(KEY)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    calls = []

    def wake(key, text, **kw):
        calls.append((key, text, kw))
        return "started"

    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 0  # settled: no duplicate
    (key, text, kw), = calls
    assert key == KEY and "report" in text and "card done" in text
    assert kw["display_kind"] == "kanban_notice" and ar.is_replay(kw["display_metadata"])
    assert kw["display_metadata"][ar.REPLAY_METADATA_KEY]["deliveries"] == 2
    assert kw["profiles"] == [None]  # tmp home is not under profiles/: the launch store, never a scan


def test_refused_admission_retains_custody_for_the_next_pass(wired, monkeypatch):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    ar._fire(KEY)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    verdicts = iter(["busy"] * 20 + ["started"])
    wake = lambda *a, **k: next(verdicts)
    for _ in range(20):
        assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 0


def test_storage_failure_keeps_local_custody_without_spending_budget(wired, monkeypatch):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    monkeypatch.setattr(Journal, "publish", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    ar._fire(KEY)
    assert ar.pending(KEY) and ar._entries[KEY].busy_rechecks == 0 and len(ar._entries[KEY].items) == 2


def test_schedule_while_already_retired_hands_off_at_once(wired, monkeypatch):
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    _arm_mixed(wired.path.parent)
    assert not ar.pending(KEY) and wired.owners() == [KEY]


def test_retirement_hook_hands_off_pending(wired, monkeypatch):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    assert ar.handoff_all() == 1 and not ar.pending(KEY) and wired.owners() == [KEY]


def test_entry_without_profile_home_stays_local_and_old_busy_budget_still_applies(wired, monkeypatch):
    ar.schedule(KEY, text="x", display_kind="kanban_notice", display_metadata={}, due=1060.0, cause="q",
                persisted=True, now=1000.0)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    assert not ar.handoff(KEY) and ar.pending(KEY)


def test_durable_success_and_stop_fences(wired):
    wired.publish(KEY, [item("persisted", created=50), item("unpersisted", created=50, persisted=False)])
    ar.cancel_durable(wired.path.parent, (KEY,), 60.0)  # a successful turn whose context started at 60
    survivors = wired.claim(KEY, 200, "h", lambda _: False)
    assert {r["id"] for r in json.loads(survivors["payload"])} == {"unpersisted"}
    ar.cancel_durable(wired.path.parent, (KEY,), 999.0, stop=True)
    assert wired.claim(KEY, 300, "h2", lambda _: False) is None


def test_cancel_durable_never_creates_tables(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    sqlite3.connect(home / "state.db").close()
    ar.cancel_durable(home, (KEY,), 1.0, stop=True)
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
