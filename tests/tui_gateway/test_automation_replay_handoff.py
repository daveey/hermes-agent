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
    from hermes_state import SessionDB
    db = SessionDB(journal.path)
    db.create_session(KEY, source="desktop")
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: journal.path.parent)
    monkeypatch.setattr(ar, "ENABLED", True)
    monkeypatch.setattr(threading, "Timer", lambda delay, fn: types.SimpleNamespace(
        start=lambda: None, daemon=False, cancel=lambda: None))
    ar.reset()
    yield journal
    ar.reset()
    db.close()


def accepted_wake(journal, key, text, kw):
    from hermes_state import SessionDB
    db = SessionDB(journal.path)
    try:
        delivery = kw["replay_delivery"]
        session = {"session_key": key}
        assert delivery.stage(db, session, text, kw["display_kind"], kw["display_metadata"])
        assert delivery.admit()
        assert journal.settle(delivery.batch)
    finally:
        db.close()
    return "started"


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
        return accepted_wake(wired, key, text, kw)

    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 0  # settled: no duplicate
    (key, text, kw), = calls
    assert key == KEY and "report" in text and "card done" in text
    assert kw["display_kind"] == "kanban_notice" and ar.is_replay(kw["display_metadata"])
    assert kw["display_metadata"][ar.REPLAY_METADATA_KEY]["deliveries"] == 2
    assert kw["profiles"] == [None]  # tmp home is not under profiles/: the launch store, never a scan


@pytest.mark.parametrize("blocked", ["busy", "refused"])
def test_refused_admission_retains_custody_for_the_next_pass(wired, monkeypatch, blocked):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    ar._fire(KEY)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    verdicts = iter([blocked] * 20 + ["started"])
    def wake(key, text, **kw):
        verdict = next(verdicts)
        return accepted_wake(wired, key, text, kw) if verdict == "started" else verdict
    for _ in range(20):
        assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 0


def test_storage_failure_keeps_local_custody_without_spending_budget(wired, monkeypatch):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    monkeypatch.setattr(Journal, "publish", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    ar._fire(KEY)
    assert ar.pending(KEY) and ar._entries[(ar._home_of(None), KEY)].busy_rechecks == 0 and len(ar._entries[(ar._home_of(None), KEY)].items) == 2


def test_schedule_while_already_retired_hands_off_at_once(wired, monkeypatch):
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    _arm_mixed(wired.path.parent)
    assert not ar.pending(KEY) and wired.owners() == [KEY]


def test_retirement_hook_hands_off_pending(wired, monkeypatch):
    _arm_mixed(wired.path.parent)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    assert ar.handoff_all() == 1 and not ar.pending(KEY) and wired.owners() == [KEY]


def test_launch_profile_chat_hands_off_and_fences_via_the_launch_home(wired, monkeypatch):
    # session records carry profile_home=None for the launch profile: that is the default case, not a leftover
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: wired.path.parent)
    ar.schedule(KEY, text="x", display_kind="kanban_notice", display_metadata={}, due=1060.0,
                cause="q", persisted=True, now=1000.0, profile_home=None)
    monkeypatch.setattr(ar, "stale_code_reason", lambda: "superseded")
    for _ in range(ar.BUSY_RECHECKS_MAX + 2):
        ar._fire(KEY)
    assert not ar.pending(KEY) and wired.owners() == [KEY]
    ar.cancel_durable(None, (KEY,), 1001.0, stop=True)  # None = launch store here too
    assert wired.claim(KEY, 5000, "h", lambda _: False) is None


def test_failing_batch_is_bounded_not_rewoken_every_pass(wired, monkeypatch):
    wired.publish(KEY, [item(due=0)])
    calls = []
    for verdict in ("error",):
        for _ in range(50):
            ar.consume_handoffs([wired.path.parent], wake=lambda *a, **k: calls.append(1) or verdict, now=5000.0)
        assert len(calls) <= ar.HANDOFF_FAILURES_MAX and not wired.owners()
        wired.publish(KEY, [item(verdict, due=0)])
        calls.clear()


def test_build_failure_with_local_replay_settles_instead_of_double_owning(wired, monkeypatch):
    wired.publish(KEY, [item(due=0)])

    def wake(owner, text, **kw):
        ar.replay_failed_wake(owner, text, {
            "profile_home": wired.path.parent,
            "agent_failure": {"failure_resets_at": ar.time.time() + 3600},
        }, kw["display_kind"], kw["display_metadata"], "build")
        return "error"

    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1
    assert not wired.owners() and ar.pending(KEY)


def test_storage_error_after_claim_does_not_strand_the_batch_or_raise(wired, monkeypatch):
    wired.publish(KEY, [item(due=0)])
    real, boom = Journal.release, {"n": 1}

    def flaky(self, batch):
        if boom["n"]:
            boom["n"] = 0
            raise sqlite3.OperationalError("database is locked")
        return real(self, batch)

    monkeypatch.setattr(Journal, "release", flaky)
    verdicts = iter(["busy", "started"])
    def wake(key, text, **kw):
        verdict = next(verdicts)
        return accepted_wake(wired, key, text, kw) if verdict == "started" else verdict
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1  # no raise
    assert ar.consume_handoffs([wired.path.parent], wake=wake, now=5000.0) == 1  # own stale claim re-claimed
    assert not wired.owners()


# --- the wiring seams the retirement fix depends on ----------------------------------------------------------------


def test_seam_retire_host_loops_publishes_pending(wired, monkeypatch):
    from hermes_cli import host_loop_lease as hl
    done = threading.Event()
    monkeypatch.setattr(ar, "handoff_all", lambda: done.set() or 1)
    monkeypatch.setattr(hl, "_RETIRED", {})
    monkeypatch.setattr(hl, "_LEASES", [])
    assert hl.retire_host_loops("t-seam", "superseded")
    assert done.wait(5)


def test_seam_run_pass_consumes_handoffs(monkeypatch):
    from tui_gateway import producer_admission as pa
    seen = []
    monkeypatch.setattr(pa, "sweep_once", lambda: None)
    monkeypatch.setattr(ar, "consume_handoffs", lambda homes, **k: seen.append(list(homes)) or 0)
    monkeypatch.setattr(pa, "_server", lambda: types.SimpleNamespace(_readopt_wake_profiles=lambda: [None, "work"]))
    lease = types.SimpleNamespace(held=lambda: True, beat=lambda: None)
    assert pa.run_pass(lease)
    assert len(seen) == 1 and len(seen[0]) == 2 and seen[0][1].name == "work"


def test_seam_wake_resumes_only_in_the_given_profile(monkeypatch):
    from tui_gateway import server
    resumed = []
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key, **kw: None)
    monkeypatch.setattr(server, "_readopt_wake_profiles", lambda: [None, "a", "b"])
    monkeypatch.setattr("tui_gateway.update_pause.left_to_successor", lambda *a, **k: False)
    monkeypatch.setattr(ar, "retiring_backend_refuses", lambda *a, **k: False)

    def resume(rid, params):
        resumed.append(params.get("profile"))
        return {"error": {"code": 4007}}

    monkeypatch.setitem(server._methods, "session.resume", resume)
    assert server._wake_session_turn(KEY, "x", label="t", profiles=["b"]) == "missing"
    assert resumed == ["b"]
    resumed.clear()
    server._wake_session_turn(KEY, "x", label="t")
    assert resumed == [None, "a", "b"]


@pytest.mark.parametrize("reason", ["user_stop", "backend_exit", "lease_takeover"])
def test_user_stop_but_not_shutdown_fences_replays(wired, monkeypatch, reason):
    from tui_gateway import server
    session = {"session_key": KEY, "profile_home": wired.path.parent,
               "history_lock": threading.RLock(), "running": False}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _: False)
    monkeypatch.setattr(server, "_clear_pending", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_cancel_background_review_on_stop", lambda *a: None)
    monkeypatch.setattr(server, "_announce_cancelled_gateway_approvals", lambda *a, **kw: None)
    monkeypatch.setattr("tools.approval.resolve_gateway_approval", lambda *a, **kw: None)
    wired.publish(KEY, [item(due=0)])
    server._interrupt_session_turn("ui", session, reason=reason)
    assert bool(wired.owners()) == (reason != "user_stop")


def test_durable_success_and_stop_fences(wired):
    wired.publish(KEY, [item("persisted", created=50), item("unpersisted", created=50, persisted=False)])
    ar.cancel_durable(wired.path.parent, (KEY,), 60.0)  # a successful turn whose context started at 60
    survivors = wired.claim(KEY, 200, "h", lambda _: False)
    assert {r["id"] for r in json.loads(survivors["payload"])} == {"unpersisted"}
    ar.cancel_durable(wired.path.parent, (KEY,), 999.0, stop=True)
    assert wired.claim(KEY, 300, "h2", lambda _: False) is None


@pytest.mark.parametrize("stop", [False, True])
def test_cancel_before_first_publication_fences_late_items(wired, stop):
    ar.cancel_durable(wired.path.parent, (KEY,), 60.0, stop=stop)
    wired.publish(KEY, [item(created=50)])
    assert wired.claim(KEY, 100, "successor", lambda _: False) is None


def test_stop_cancels_local_unpersisted_delivery(wired):
    ar.schedule(KEY, text="x", display_kind="kanban_notice", display_metadata={}, due=100,
                cause="q", persisted=False, now=50, profile_home=wired.path.parent)
    ar.cancel_durable(wired.path.parent, (KEY,), 60, stop=True)
    assert not ar.pending(KEY)


def test_unrelated_local_retry_does_not_dispose_of_failed_handoff(wired):
    wired.publish(KEY, [item(due=0)])
    ar.schedule(KEY, text="other", display_kind="kanban_notice", display_metadata={}, due=9e9,
                cause="q", persisted=False, now=50, profile_home=wired.path.parent)
    ar.consume_handoffs([wired.path.parent], wake=lambda *a, **k: "error", now=5000)
    assert wired.owners() == [KEY]


def test_named_launch_profile_is_consumed(monkeypatch, tmp_path):
    from tui_gateway import producer_admission as pa
    home = tmp_path / "profiles" / "work"
    seen = []
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: home)
    monkeypatch.setattr(pa, "_server", lambda: types.SimpleNamespace(_readopt_wake_profiles=lambda: [None]))
    monkeypatch.setattr(ar, "consume_handoffs", lambda homes, **kw: seen.extend(homes))
    pa._consume_replay_handoffs()
    assert seen == [home]
