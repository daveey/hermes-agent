"""Missing Board rows must not be described as capacity waits."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import test_kanban_sync_run_rows as helpers

kanban_home = helpers.kanban_home
svc = helpers.svc
live = helpers.live
REJECT = "forbidden_field: not writable on tasks: model_pin_mandatory"


def _card(conn):
    tid = kb.create_task(conn, title="new card", assignee="worker")
    old = int(time.time()) - 31 * 60
    conn.execute("UPDATE tasks SET session_id='origin', created_at=? WHERE id=?", (old, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (old, tid))
    kb._append_event(conn, tid, "sync_rejected", {"reason": REJECT})
    conn.commit()
    return tid


def _notice(conn, tid):
    assert cw.sweep(conn) == 1
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, cw.NOTICE_KIND)).fetchone()[0]
    return cw.notice_text(json.loads(payload))


@pytest.fixture(autouse=True)
def missing_claim(live, monkeypatch):
    monkeypatch.setattr(sync._client(), "acquire", lambda **kw: sync.Lease(
        granted=False, run_uid=kw["run_uid"], reason="not_found"))


def test_missing_board_claim_produces_durable_reason_not_priority_advice(live):
    tid = _card(live)
    assert sync.claim_gate(live, tid, int(time.time()) + 900).deferred
    # Dispatcher consumes the process-local hint; notice must survive that and another tick.
    assert sync.pop_claim_deferral(tid) == "not_found"
    assert sync.claim_gate(live, tid, int(time.time()) + 900).deferred
    text = _notice(live, tid)
    assert "isn't on the shared Board" in text
    assert REJECT in text
    assert "priority" not in text and "re-place" not in text
    assert live.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='claim_deferred'", (tid,)).fetchone()[0] == 1


@pytest.mark.parametrize("reason", ["capacity", "offline"])
def test_changed_claim_reason_does_not_reuse_missing_board_diagnosis(live, monkeypatch, reason):
    tid = _card(live)
    assert sync.claim_gate(live, tid, int(time.time()) + 900).deferred
    def acquire(**kwargs):
        if reason == "offline":
            raise OSError("offline")
        return SimpleNamespace(granted=False, reason=reason, holder=None)
    monkeypatch.setattr(sync._client(), "acquire", acquire)
    assert sync.claim_gate(live, tid, int(time.time()) + 900).deferred
    text = _notice(live, tid)
    assert "isn't on the shared Board" not in text
    assert "priority" in text


def test_real_capacity_wait_retains_priority_advice(live):
    tid = _card(live)
    assert "priority" in _notice(live, tid)


@pytest.mark.parametrize("change", ["old_episode", "foreign_gateway", "sync_off", "granted"])
def test_stale_or_foreign_missing_row_evidence_is_not_a_current_diagnosis(live, monkeypatch, change):
    tid = _card(live)
    assert sync.claim_gate(live, tid, int(time.time()) + 900).deferred
    if change == "old_episode":
        live.execute("UPDATE task_events SET created_at=0 WHERE task_id=? AND kind='claim_deferred'", (tid,))
    elif change == "foreign_gateway":
        live.execute("UPDATE task_events SET payload=json_set(payload, '$.gateway', 'other') "
                     "WHERE task_id=? AND kind='claim_deferred'", (tid,))
    elif change == "sync_off":
        sync.set_mode(live, "off")
    else:
        monkeypatch.setattr(sync._client(), "acquire", lambda **kw: sync.Lease(
            granted=True, run_uid=kw["run_uid"], expires_ms=int(time.time() * 1000) + 900000))
        with sync.claim_gate(live, tid, int(time.time()) + 900) as gate:
            assert not gate.deferred
    live.commit()
    text = _notice(live, tid)
    assert "isn't on the shared Board" not in text
    assert "priority" in text


def test_push_rejection_reaches_missing_board_notice(live):
    from hermes_cli import kanban_sync_client as client
    tid = _card(live)
    live.execute("DELETE FROM task_events WHERE task_id=? AND kind='sync_rejected'", (tid,))
    def ops(board, batch):
        return {"results": [
            {"client_op_id": op["client_op_id"], "status": "rejected", "reason": "forbidden_field",
             "detail": "not writable on tasks: model_pin_mandatory"}
            if op["table"] == "tasks" else {"client_op_id": op["client_op_id"], "status": "applied"}
            for op in batch]}
    client.push(live, SimpleNamespace(ops=ops), "default", "macbook", primary=True)
    assert sync.claim_gate(live, tid, int(time.time()) + 900).deferred
    assert REJECT in _notice(live, tid)


def test_new_episode_records_a_repeated_missing_row_denial(live):
    tid = _card(live)
    sync.claim_gate(live, tid, int(time.time()) + 900)
    live.execute("UPDATE task_events SET created_at=0 WHERE task_id=? AND kind='claim_deferred'", (tid,))
    sync.claim_gate(live, tid, int(time.time()) + 900)
    assert "isn't on the shared Board" in _notice(live, tid)


def test_unknown_rejection_is_explicit_and_newest_rejection_wins(live):
    tid = _card(live)
    live.execute("DELETE FROM task_events WHERE task_id=? AND kind='sync_rejected'", (tid,))
    sync.claim_gate(live, tid, int(time.time()) + 900)
    from hermes_cli.kanban_sync_diagnostics import missing_board
    assert missing_board(live, tid, 0)["sync_reject_reason"] == "no sync rejection recorded"
    kb._append_event(live, tid, "sync_rejected", {"reason": "first"})
    kb._append_event(live, tid, "sync_rejected", {"reason": REJECT})
    assert missing_board(live, tid, 0)["sync_reject_reason"] == REJECT
