"""Composed delivery/recovery must retain custody and eventual progress. [daveey]

S1: actual hub label delivery, including destination-pending replicas.
S2: retained shared-blocker sweep after a genuine exact source abort.
S3: caused-stop review with an unfrozen source and frozen wait_on target.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as transfer
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_home_release_candidates as support

board = support.board
NONCE = "release-candidates-0001"


def unfreeze(conn, task, phase):
    """Genuine release: trusted Board abort for prepared, authenticated feed unpend for pending."""
    with dbc.write_txn(conn):
        if phase == "prepared":
            transfer.abort_source(conn, {"task": task, "source": "worker-a3", "destination": "hub", "nonce": NONCE,
                                         "expected_version": 7, "phase": "aborted", "rows": []})
        else:
            from hermes_cli.kanban_home_generation import mirror_feed
            mirror_feed(conn, task, {"home_generation": NONCE, "home_transfer_pending": None}, 9)
    assert not transfer.frozen(conn, task)


# ---------------------------------------------------------------- S1 label delivery
def _label_job(conn, task, event):
    from hermes_cli import kanban_operator_labels as labels
    with dbc.write_txn(conn):
        for sql in labels.SCHEMA:
            conn.execute(sql)
        conn.execute("INSERT INTO operator_label_jobs(request_uid,board,machine,labels,caller,result_task_id,"
                     "state,outcome,detail,event_uid,created_at,delivery_attempts) "
                     "VALUES ('request','default','worker-a3','[]','{}',?,'done','verified','ok',?,1,0)",
                     (task, event))


def _fake_label_hub(monkeypatch, task, event, sent, epoch):
    def evidence(_board, table, _uid):
        if table == "task_events":
            return {"state": "live", "row": {**sent[-1]["fields"], "uid": event, "origin_gateway": "hub"}}
        return {"state": "live", "row": {"id": task, "home_generation": epoch, "home_transfer_pending": None}}

    def ops(_board, batch):
        sent.extend(batch)
        return {"results": [{"status": "applied", "client_op_id": batch[0]["client_op_id"]}]}
    hub = SimpleNamespace(row_evidence=evidence, ops=ops, close=lambda: None)
    monkeypatch.setattr(client, "load_identity", lambda: object())
    monkeypatch.setattr(client, "Hub", lambda _ident: hub)


def _label_freeze(conn, task, phase):
    if phase != 'pending':
        return support.freeze(conn, task, phase)
    # The actual label executor is the hub, holding a destination-pending replica.
    from hermes_cli.kanban_home_generation import mirror_feed
    with dbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET gateway_id='hub'")
        conn.execute("UPDATE tasks SET created_on='hub',sync_version=8 WHERE id=?", (task,))
        conn.execute('DELETE FROM kanban_sync_outbox')
        mirror_feed(conn, task, {'home_generation': NONCE, 'home_transfer_pending': NONCE}, 8)


def _job(conn):
    return dict(conn.execute("SELECT delivery_attempts,delivered_at,last_error FROM operator_label_jobs").fetchone())


def _capture(conn, event):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_label_capture1'").fetchone():
        return None
    row = conn.execute("SELECT task_id,legacy FROM kanban_home_label_capture1 WHERE event_uid=?", (event,)).fetchone()
    return None if row is None else tuple(row)


@pytest.mark.parametrize("phase", ["prepared", "pending", None])
def test_s1_frozen_delivery_then_release_keeps_epoch(board, monkeypatch, phase):
    from hermes_cli import kanban_operator_labels as labels
    task = support.card(board, "ready")
    event = "review4300-label-event-0001"
    _label_job(board, task, event)
    sent, epoch = [], "epoch-release-target-0001"
    _fake_label_hub(monkeypatch, task, event, sent, epoch)
    if phase:
        _label_freeze(board, task, phase)
        assert transfer.frozen(board, task)
        out = labels._deliver(board, "hub", "default", now=10)
        after_frozen = _job(board)
        print("S1", phase, "frozen tick sent=", out, "job=", after_frozen, "capture=", _capture(board, event),
              "ops=", len(sent))
        assert out == 0 and not sent
        unfreeze(board, task, phase)
    out = labels._deliver(board, "hub", "default", now=20)
    print("S1", phase, "healthy tick sent=", out, "job=", _job(board), "capture=", _capture(board, event),
          "wire_home_generations=", sent[0].get("home_generations") if sent else "<none sent>")
    assert out == 1
    assert sent[0].get("home_generations") == {task: epoch}, (
        "frozen pre-capture refusal reclassified a new-producer job as legacy: epoch dropped")


@pytest.mark.parametrize("phase", ["prepared", "pending"])
def test_s1_frozen_delivery_cap_marks_delivered_without_send(board, monkeypatch, phase):
    from hermes_cli import kanban_operator_labels as labels
    task = support.card(board, "ready")
    event = "review4300-label-event-0002"
    _label_job(board, task, event)
    sent = []
    _fake_label_hub(monkeypatch, task, event, sent, "epoch-cap-0001")
    _label_freeze(board, task, phase)
    now, ticks = 10, 0
    while _job(board)["delivered_at"] is None and ticks < labels.MAX_DELIVERY_ATTEMPTS + 5:
        labels._deliver(board, "hub", "default", now=now)
        now += labels.DELIVERY_BACKOFF_CAP_S + 1
        ticks += 1
    job = _job(board)
    print("S1cap", phase, "ticks=", ticks, "job=", job, "ops_sent=", len(sent), "still_frozen=",
          transfer.frozen(board, task))
    assert transfer.frozen(board, task)
    assert not sent
    assert job['delivery_attempts'] == 0
    assert job["delivered_at"] is None, "label result abandoned (delivered_at set) while transfer only pending"
    unfreeze(board, task, phase)
    assert labels._deliver(board, 'hub', 'default', now=now) == 1
    assert len(sent) == 1 and sent[0]['home_generations'] == {task: 'epoch-cap-0001'}


# ---------------------------------------------------------------- S2 gated shared-blocker sweep
class _Result:
    shared_blocker_probed = shared_blocker_released = None


def _stranded(conn):
    from unittest import mock
    from hermes_cli import kanban_timed_park as ktp
    with mock.patch.object(ktp, "park_mode", lambda *a, **k: None):
        tid = kb.create_task(conn, title="stranded", assignee="default")
        assert kb.claim_task(conn, tid) is not None
        assert kb.block_task(conn, tid, reason="GitHub Actions refuses every job: billing",
                             kind="capability", blocker_id="review4300-wall")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at=started_at-7200, "
                     "ended_at=CASE WHEN ended_at IS NULL THEN NULL ELSE ended_at-7200 END")
        conn.execute("UPDATE task_events SET created_at=created_at-7200")
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _sweep(conn):
    from hermes_cli import kanban_idle_candidates as idle
    r = _Result()
    idle.sweep_shared_blockers(conn, r)
    return r.shared_blocker_probed


@pytest.mark.parametrize("phase", ["prepared", "pending", None])
def test_s2_gated_probe_after_genuine_release(board, phase):
    from hermes_cli import kanban_sweep_gate as gate
    from hermes_cli.kanban_activity_revision import LOCAL_TABLES
    task = _stranded(board)
    gate.reset()
    if phase is None:
        out = _sweep(board)
        print("S2", phase, "control probed=", out, "status=", kb.get_task(board, task).status)
        assert out == [task]
        return
    support.freeze(board, task, phase)
    assert transfer.frozen(board, task)
    first = _sweep(board)
    k1 = gate.key(board)
    print("S2", phase, "warm frozen probed=", first, "status=", kb.get_task(board, task).status, "key=", k1)
    assert first == [] and kb.get_task(board, task).status == "blocked"
    assert k1 is not None, "gate key unknown: sweep never skips (refutes S2)"
    unfreeze(board, task, phase)
    k2 = gate.key(board)
    second = _sweep(board)
    print("S2", phase, "after release key_equal=", k1 == k2, "probed=", second, "status=",
          kb.get_task(board, task).status, "custody tables in key=",
          [t for t in ("kanban_home_aborted1", "kanban_home_generations1") if t in LOCAL_TABLES])
    gate.reset()
    diag = _sweep(board)
    print("S2", phase, "diagnostic after gate.reset probed=", diag, "status=", kb.get_task(board, task).status)
    assert second == [task], "warm gated sweep stayed skipped after genuine release"
    assert diag is None, "no blocked candidate remains after release"


# ---------------------------------------------------------------- S3 caused-stop wait_on frozen target
@pytest.fixture
def hub(monkeypatch):
    import hashlib
    from hermes_cli import kanban_db_sync as sync
    calls = []

    class Hub:
        ident = SimpleNamespace(gateway="home")

        def decision_retain_capability(self, _board):
            return {"spend_review": 1}

        def ops(self, _board, batch):
            calls.append(batch[0])
            return {"results": [{"client_op_id": batch[0]["event_uid"], "status": "applied"}]}

        def row_evidence(self, board, table, uid):
            if not calls:
                return {'state': 'missing'}
            op = calls[-1]
            payload = {k: op[k] for k in ("task", "episode", "verdict", "reason", "max_grants")}
            payload.update(version=1, runs=None, quote=op.get("quote"), target=op.get("target"), request_uid=uid,
                           request_sha256=hashlib.sha256(json.dumps(
                               {k: v for k, v in op.items() if k != 'home_generations'},
                               sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                           actor_kind="system", actor_session=None, actor_gateway="home",
                           authority_kind="spend_reviewer", authority_ref="kanban-spend-reviewer")
            row = dict(uid=uid, task_id=op["task"], kind="spend_review", version=1, origin_gateway="home",
                       deleted_at=None, payload=json.dumps(payload))
            return {"board": board, "table": table, "uid": uid, "state": "live", "row": row}

        def close(self):
            pass
    monkeypatch.setattr(sync, "sync_state", lambda c: {"board_id": "test"})
    monkeypatch.setattr(sync, "board_mode", lambda c: "primary")
    monkeypatch.setattr(client, "load_identity", lambda: None)
    monkeypatch.setattr(client, "Hub", lambda ident, **kw: Hub())
    monkeypatch.setattr(client, "push", lambda *a, **kw: None)
    return calls


@pytest.mark.parametrize("phase", ["prepared", "pending", None])
def test_s3_wait_on_frozen_target_escalates_source(board, spend_reviewer, hub, monkeypatch, phase):
    from hermes_cli import kanban_spend_reviewer as reviewer
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli import kanban_spend_review_request as journal
    target = support.card(board, "ready")
    source = kb.create_task(board, title="caused stop source", assignee="default")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (source,))
        kb._append_event(board, source, "spend_budget_triaged",
                         {"cause": "scope_stopped", "reason": f"Stopped until {target} lands."})
    ep = reviewer._current_episode(board, source)
    with kb.write_txn(board):
        kb._append_event(board, source, decision.NOTICE, {"episode": ep})
        kb._append_event(board, source, reviewer.STARTED, {"episode": ep})
    if phase:
        support.freeze(board, target, phase)
        assert transfer.frozen(board, target) and not transfer.frozen(board, source)
    spend_reviewer.append({"decision": "wait_on", "reason": f"Blocked on {target}.", "target": target})
    out = reviewer.review(board, source, ep)
    events = [r[0] for r in board.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (source,))]
    print("S3", phase, "review=", out, "state=", reviewer.state(board, source, ep), "journal=",
          journal.get(board, source, ep) is not None, "ops=", len(hub), "source_events=", events[-4:])
    if phase is None:
        assert out == "settled" and len(hub) == 1
        return
    assert not hub, "no submission while target frozen"
    assert out == 'pending'
    retained = journal.get(board, source, ep)
    assert retained['capture_pending'] is True
    from hermes_cli import kanban_home_command as home
    with pytest.raises(home.Pending):
        home.outbound(retained, retained['wire'])
    import sqlite3
    path = board.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as reopened:
        assert journal.get(reopened, source, ep) == retained
    original_wire = retained['wire']
    assert reviewer.review(board, source, ep) == 'pending'
    assert journal.get(board, source, ep) == retained
    assert not hub and not spend_reviewer  # consumed exactly one judgment, no replacement
    assert reviewer.start_due(board, now=10**10) == []
    unfreeze(board, target, phase)
    with monkeypatch.context() as held:
        held.setattr(reviewer, 'settings', lambda: (False, 2))
        assert reviewer.review(board, source, ep) == 'pending'
        assert journal.get(board, source, ep) == retained and not hub
    assert reviewer.review(board, source, ep) == 'settled'
    captured = journal.get(board, source, ep)
    assert 'capture_pending' not in captured
    assert captured['wire'] == original_wire
    assert len(hub) == 1
    assert {k: v for k, v in hub[0].items() if k != 'home_generations'} == original_wire
    assert kb.get_task(board, target).status == 'ready'
    assert reviewer.review(board, source, ep) == 'moot'
    assert len(hub) == 1
