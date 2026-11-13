"""Durable, change-only placement observations; never an execution grant."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_queue_reason as qr
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures
from tests.hermes_cli import test_kanban_queue_reason_hold as hold

no_comment_hold = hold.no_comment_hold
conn = fixtures.conn


def reply(code="admission_limits", at=1000, reasons=None):
    return {"state": "queued", "machine": None, "reason": "waiting_for_worker_capacity",
            "observation": {"version": 1, "code": code, "observed_at_ms": at,
                            "candidates": [{"machine": "worker-1", "reasons": reasons or ["load_threshold"]}]}}


@pytest.mark.usefixtures("no_comment_hold")
def test_reason_persists_once_and_recovers(conn):
    tid = fixtures.acknowledged(conn)
    assert qr.record_reply(conn, tid, "hub", reply())
    assert not qr.record_reply(conn, tid, "hub", reply(at=2000))
    assert len(kb.list_comments(conn, tid)) == 1
    assert qr.latest(conn, tid)["code"] == "admission_limits"
    assigned = {**reply("reserved"), "state": "assigned", "machine": "worker-1"}
    assert qr.record_reply(conn, tid, "hub", assigned)
    assert len(kb.list_comments(conn, tid)) == 2
    assert qr.latest(conn, tid)["code"] == "reserved"
    assert conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None


@pytest.mark.usefixtures("no_comment_hold")
def test_legacy_and_board_errors_are_honest(conn):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", {"state": "queued", "reason": "waiting_for_worker_capacity"})
    assert qr.latest(conn, tid)["code"] == "observation_unavailable"
    qr.record_error(conn, tid, "hub", status=500)
    assert qr.latest(conn, tid)["code"] == "board_error"
    assert "500" in kb.list_comments(conn, tid)[-1].body
    assert not qr.record_error(conn, tid, "hub", status=500)


def test_foreign_and_running_copies_never_write(conn):
    tid = fixtures.acknowledged(conn)
    assert not qr.record_reply(conn, tid, "other", reply())
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    conn.commit()
    assert not qr.record_reply(conn, tid, "hub", reply())
    assert not kb.list_comments(conn, tid)


def test_latest_qualifies_historical_wait(conn):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", reply())
    conn.execute("UPDATE tasks SET placement_policy=NULL WHERE id=?", (tid,))
    conn.commit()
    assert qr.latest(conn, tid)["current"] is False
    assert "historical" in qr.describe(qr.latest(conn, tid))


def test_candidate_order_and_replay_do_not_repeat_comments(conn):
    tid = fixtures.acknowledged(conn)
    r = reply(reasons=["load_threshold", "memory_threshold"])
    qr.record_reply(conn, tid, "hub", r)
    r["observation"]["replay"] = True
    r["observation"]["candidates"][0]["reasons"].reverse()
    assert not qr.record_reply(conn, tid, "hub", r)
    assert len(kb.list_comments(conn, tid)) == 1


def test_unknown_payload_never_persists_raw_fields(conn):
    tid = fixtures.acknowledged(conn)
    r = reply()
    r["observation"]["secret"] = "DO_NOT_STORE"
    qr.record_reply(conn, tid, "hub", r)
    assert "DO_NOT_STORE" not in json.dumps(qr.latest(conn, tid))


@pytest.mark.parametrize("code", sorted(qr.CODES - {"board_error", "no_eligible_worker"}))
def test_each_reason_has_durable_matching_thread_and_change_only_dedupe(conn, code):
    tid = fixtures.acknowledged(conn)
    r = reply(code, reasons=[code])
    r["observation"]["candidates"][0]["missing_labels"] = ["git", "file"]
    assert qr.record_reply(conn, tid, "hub", r)
    latest = qr.latest(conn, tid)
    assert latest["code"] == code
    assert latest["observed_at_ms"] == 1000
    assert kb.list_comments(conn, tid)[-1].body == qr.describe(latest)
    r["observation"]["observed_at_ms"] = 2000
    r["observation"]["candidates"][0]["cpu_load1"] = 99
    assert not qr.record_reply(conn, tid, "hub", r)
    assert len(kb.list_comments(conn, tid)) == 1
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks' AND state='pending'").fetchone()


def test_no_eligible_worker_thread_is_the_single_repin_notice(conn):
    """``no_eligible_worker`` keeps the durable change-only observation, but its one thread comment is the owner's
    repin notice (kanban_repin_notice), not a second diagnostic line beside it."""
    tid = fixtures.acknowledged(conn)
    r = reply("no_eligible_worker", reasons=["missing_labels"])
    r["observation"]["candidates"][0]["missing_labels"] = ["skill:five-whys"]
    assert qr.record_reply(conn, tid, "hub", r)
    assert qr.latest(conn, tid)["code"] == "no_eligible_worker"
    bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert len(bodies) == 1 and bodies[0].startswith("No pool worker can run this card (needs: skill:five-whys)")
    r["observation"]["observed_at_ms"] = 2000
    assert not qr.record_reply(conn, tid, "hub", r)
    assert len(kb.list_comments(conn, tid)) == 1


@pytest.mark.parametrize("fields", [
    {"created_on": "foreign"}, {"claim_lock": "claim"}, {"lease_holder": "worker-1"},
    {"status": "blocked"}, {"placement_policy": None},
])
def test_record_fences_current_row(conn, fields):
    tid = fixtures.acknowledged(conn, **fields)
    assert not qr.record_error(conn, tid, "hub", status=500)
    assert not qr.record_reply(conn, tid, "hub", reply())
    assert qr.latest(conn, tid) is None
    assert not kb.list_comments(conn, tid)


@pytest.mark.parametrize("state_change", [
    "runs_on='[\"machine:worker-1\"]'", "claim_lock='claim'", "lease_holder='worker-1'",
    "status='running'", "status='done'", "placement_policy=NULL",
])
def test_lifecycle_changes_do_not_present_old_wait_as_current(conn, state_change):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", reply("draining", reasons=["draining"]))
    conn.execute(f"UPDATE tasks SET {state_change} WHERE id=?", (tid,))
    conn.commit()
    assert qr.latest(conn, tid)["current"] is False
    assert "historical" in qr.describe(qr.latest(conn, tid))


def test_latest_uses_origin_record_time_not_replica_insertion_order(conn):
    tid = fixtures.acknowledged(conn)
    for code, recorded in [("reserved", 2002), ("draining", 2001)]:
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                     (tid, qr.EVENT, json.dumps({"code": code, "recorded_at_ms": recorded}), 2))
    conn.commit()
    assert qr.latest(conn, tid)["code"] == "reserved"


def test_same_millisecond_transitions_have_strict_record_order(conn, monkeypatch):
    tid = fixtures.acknowledged(conn)
    monkeypatch.setattr(qr.time, "time", lambda: 1234)
    qr.record_reply(conn, tid, "hub", reply())
    before = qr.latest(conn, tid)["recorded_at_ms"]
    qr.record_reply(conn, tid, "hub", reply("no_live_candidate"))
    assert qr.latest(conn, tid)["recorded_at_ms"] > before


def test_drain_bounds_are_explicit_and_never_inferred(conn):
    tid = fixtures.acknowledged(conn)
    r = reply("draining", reasons=["draining"])
    qr.record_reply(conn, tid, "hub", r)
    assert "drain end time unknown" in qr.describe(qr.latest(conn, tid))
    r["observation"]["candidates"][0]["drain_until_ms"] = 5000
    qr.record_reply(conn, tid, "hub", r)
    assert "producer-announced drain deadline" in qr.describe(qr.latest(conn, tid))
    assert "not guaranteed" in qr.describe(qr.latest(conn, tid))


def test_completed_attempt_does_not_revive_old_wait_on_ready_retry(conn):
    tid = fixtures.acknowledged(conn)
    qr.record_reply(conn, tid, "hub", reply())
    conn.execute("INSERT INTO task_runs(task_id, started_at, status) VALUES (?, 1, 'done')", (tid,))
    conn.commit()
    assert qr.latest(conn, tid)["current"] is False
    assert qr.record_reply(conn, tid, "hub", reply())
    assert qr.latest(conn, tid)["current"] is True


def test_matching_authoritative_assignment_remains_current_without_duplicate(conn):
    tid = fixtures.acknowledged(conn)
    r = {**reply("reserved"), "state": "assigned", "machine": "worker-1"}
    qr.record_reply(conn, tid, "hub", r)
    conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", ('["machine:worker-1"]', tid))
    conn.commit()
    assert qr.latest(conn, tid)["current"] is True
    assert not qr.record_reply(conn, tid, "hub", r)


def test_wrong_sync_identity_cannot_record_origin_observation(conn):
    tid = fixtures.acknowledged(conn)
    conn.execute("UPDATE kanban_sync_state SET gateway_id='replica'")
    conn.commit()
    assert not qr.record_reply(conn, tid, "hub", reply())
    assert not qr.record_reply(conn, tid, "replica", reply())
    assert not kb.list_comments(conn, tid)


def test_comment_failure_rolls_back_observation(conn, monkeypatch):
    tid = fixtures.acknowledged(conn)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic comment failure")

    monkeypatch.setattr(kb, "add_comment", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        qr.record_reply(conn, tid, "hub", reply())
    assert qr.latest(conn, tid) is None


@pytest.mark.parametrize("candidates", [
    [{"machine": "worker-1", "reasons": ["last_seen_stale", "metrics_stale"]}],  # probe t_01a19fd990770095, 11-03
    [{"machine": "worker-1", "reasons": ["load_threshold"]}, {"machine": "worker-2", "reasons": ["draining"]}],
])
def test_mixed_wording_is_true_for_one_or_many_candidates(conn, candidates):
    tid = fixtures.acknowledged(conn)
    r = reply("mixed")
    r["observation"]["candidates"] = candidates
    assert qr.record_reply(conn, tid, "hub", r)
    body = kb.list_comments(conn, tid)[-1].body
    assert body.startswith("Candidates excluded for multiple reasons — worker-1: ")
    assert "different exclusions" not in body
