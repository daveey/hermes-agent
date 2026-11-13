"""A worker whose owner stops answering mid-turn is not a card failure. [daveey]

paintbot-tools/t_01a197166756bd53 run 207 (hermes-hub, 2026-11-13 20:06Z): the owner gateway, wedged
after an aborted core-update drain, never answered the worker's ``transcript.append``; the resend's
rediscovery timed out and the bound interpreter died with ``WorkerPersistenceError: owner_unavailable``.
The sweep booked it ``crashed`` and charged the card, unlike the sibling ``storage_unavailable``.
"""
import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_host_refusal as helpers

home = helpers.home

_TERMINAL = "Worker failed: WorkerPersistenceError: owner_unavailable"
# The turn's own failed result, recorded before the outer worker's exception envelope (run 207, event 10270).
_TURN_TEXT = ("⚠️ No reply: Hermes couldn't save this conversation, so it stopped rather than lose your "
              "messages.")


def _result(conn, tid, **changes):
    task = kb.get_task(conn, tid)
    payload = {"pid": os.getpid(), "claim_lock": task.claim_lock, "exit_code": 1, "last_output": _TERMINAL}
    payload.update(changes)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_result", payload, run_id=task.current_run_id)


def test_midrun_owner_unreachable_requeues_without_spending_budget(home):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries=1 WHERE id=?", (tid,))
        _result(conn, tid, last_output=_TURN_TEXT)
        _result(conn, tid)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        run = helpers._runs(conn, tid)[-1]
        assert run["outcome"] == "owner_unavailable"
        assert "turn interrupted" in run["error"] and "no turn ran" not in run["error"]
        assert "could not reach its profile owner" in run["error"]
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
        (notice,) = helpers._notices(conn)
        assert "could not reach its profile owner" in notice["body"]


@pytest.mark.parametrize("output", [
    "Worker failed: WorkerPersistenceError: owner_unavailable_extra",
    "Worker failed: RuntimeError: owner_unavailable",
    "quoted " + _TERMINAL,
    _TERMINAL + "\nAnother failure",
])
def test_inexact_envelope_still_counts(home, output):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, last_output=output)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert not helpers._notices(conn)


def test_later_card_fault_wins(home):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid)
        _result(conn, tid, last_output="Worker failed: ValueError: real card fault")
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


@pytest.mark.parametrize("field", ["claim_lock", "pid", "exit_code"])
def test_receipt_not_tied_to_the_bound_claim_counts(home, field):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, **{field: {"claim_lock": "other:1", "pid": 1, "exit_code": 2}[field]})
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


@pytest.mark.parametrize("field,value", [("claim_lock", "other:1"), ("pid", 1)])
def test_legacy_result_from_another_claim_or_pid_is_not_proof(home, field, value):
    from hermes_cli import kanban_owner_storage_refusal as storage
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        task = kb.get_task(conn, tid)
        _result(conn, tid, **{field: value})
        # Call the classifier directly: the sweep's separate identity gate otherwise masks this guard.
        assert storage.proven(conn, tid, task.current_run_id, task.claim_lock) is None


@pytest.mark.parametrize("exit_code", [0, 2, 75, None])
def test_legacy_nonfailure_exit_is_not_owner_unreachable_proof(home, exit_code):
    from hermes_cli import kanban_owner_storage_refusal as storage
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        task = kb.get_task(conn, tid)
        _result(conn, tid, exit_code=exit_code)
        # Pin _proof's defence independently of owner_unavailable's dead.code == 1 gate.
        assert storage.proven(conn, tid, task.current_run_id, task.claim_lock) is None


def _modern_result(conn, tid, output):
    from hermes_cli.kanban_failed_result_identity import result_identity_fields
    task = kb.get_task(conn, tid)
    context = {"task_id": tid, "run_id": task.current_run_id}
    with kb.write_txn(conn):
        fields = result_identity_fields(conn, context)
        kb._append_event(conn, tid, "worker_result", {
            "pid": os.getpid(), "claim_lock": task.claim_lock, "exit_code": 1,
            "last_output": output, "death_proof_version": 1, **fields,
        }, run_id=task.current_run_id)


@pytest.mark.parametrize("output,owner_fault", [(_TERMINAL, True), (_TURN_TEXT, False)])
def test_modern_receipt_midrun_owner_unreachable_requeues_without_spending_budget(home, monkeypatch, output, owner_fault):
    from hermes_cli import kanban_host_refusal as hr
    from hermes_cli import kanban_owner_storage_refusal as storage
    from hermes_cli import kanban_failed_result as failed
    from hermes_cli.kanban_failed_result_identity import modern_receipt
    # This fixture's interpreter is still running; model its post-exit observation, not host PIDs.
    monkeypatch.setattr(failed, "process_gone", lambda pid, birth: True)
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        task = kb.get_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries=1 WHERE id=?", (tid,))
        # Run 207 emitted the turn result before the terminal exception envelope.
        _modern_result(conn, tid, _TURN_TEXT)
        _modern_result(conn, tid, output)
        modern, selected = modern_receipt(conn, tid, task.current_run_id, task.claim_lock)
        assert modern and selected is not None
        receipt = selected[1]
        assert receipt["death_proof_version"] == 1 and receipt["result_sequence"] == 2
        bound = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='worker_bound'", (tid,)).fetchone()
        identity = {key: json.loads(bound["payload"])[key] for key in ("pid", "started_at", "claim_lock")}
        if "uid" in bound.keys() and bound["uid"]:
            identity["uid"] = bound["uid"]
        assert receipt["bound_event_identity"] == identity
        expected = {"kind": hr.OWNER_UNREACHABLE, "phase": "midrun", "reason": "owner_unavailable"}
        assert storage.proven(conn, tid, task.current_run_id, task.claim_lock) == (expected if owner_fault else None)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == (0 if owner_fault else 1)
        run = helpers._runs(conn, tid)[-1]
        assert run["outcome"] == ("owner_unavailable" if owner_fault else "crashed")
        if owner_fault:
            assert task.status == "ready"
            assert "turn interrupted" in run["error"] and "no turn ran" not in run["error"]
            assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
            (notice,) = helpers._notices(conn)
            assert "could not reach its profile owner" in notice["body"]
        else:
            assert not helpers._notices(conn)


def test_unreachable_owner_streak_still_reaches_the_breaker(home):
    from hermes_cli import kanban_owner_unavailable as ou
    with kbc.connect() as conn:
        tid = None
        for i in range(ou.NEUTRAL_STREAK_LIMIT + 1):
            tid, _ = helpers._admitted_run(conn, tid, submitter=73000 + i)
            _result(conn, tid)
            kbd.detect_crashed_workers(conn)
        runs = helpers._runs(conn, tid)
        assert [r["outcome"] for r in runs[:-1]] == ["owner_unavailable"] * ou.NEUTRAL_STREAK_LIMIT
        assert runs[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_storage_and_unreachable_proofs_are_distinct_kinds(home):
    from hermes_cli import kanban_host_refusal as hr
    from hermes_cli import kanban_owner_storage_refusal as storage
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid)
        task = kb.get_task(conn, tid)
        proof = storage.proven(conn, tid, task.current_run_id, task.claim_lock)
        assert proof == {"kind": hr.OWNER_UNREACHABLE, "phase": "midrun", "reason": "owner_unavailable"}


# Sibling path: the resend's redial reached the gateway, but send 2/2 still got no receipt or the
# owner dropped the link again (``runtime_session_store.exhausted_resend_code``).
_SILENT_RESEND = ["worker_receipt_timeout", "owner_link_closed"]


@pytest.mark.parametrize("code", _SILENT_RESEND)
def test_exhausted_resend_requeues_without_spending_budget(home, code):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries=1 WHERE id=?", (tid,))
        _result(conn, tid, last_output=_TURN_TEXT)
        _result(conn, tid, last_output=f"Worker failed: WorkerPersistenceError: {code}")
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        run = helpers._runs(conn, tid)[-1]
        assert run["outcome"] == "owner_unavailable"
        assert f"could not reach its profile owner to save the turn ({code}:" in run["error"]
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()


@pytest.mark.parametrize("code", _SILENT_RESEND)
def test_exhausted_resend_streak_still_reaches_the_breaker(home, code):
    from hermes_cli import kanban_owner_unavailable as ou
    with kbc.connect() as conn:
        tid = None
        for i in range(ou.NEUTRAL_STREAK_LIMIT + 1):
            tid, _ = helpers._admitted_run(conn, tid, submitter=74000 + i)
            _result(conn, tid, last_output=f"Worker failed: WorkerPersistenceError: {code}")
            kbd.detect_crashed_workers(conn)
        runs = helpers._runs(conn, tid)
        assert [r["outcome"] for r in runs[:-1]] == ["owner_unavailable"] * ou.NEUTRAL_STREAK_LIMIT
        assert runs[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


@pytest.mark.parametrize("output", [
    "Worker failed: TimeoutError: worker_receipt_timeout",  # not the store's latched envelope
    "Worker failed: WorkerPersistenceError: worker_receipt_timeout_extra",
    # A close code that may answer this frame keeps its raw text and stays the card's.
    "Worker failed: WorkerPersistenceError: received 1009 (message too big); then sent 1009 (message too big)",
    "Worker failed: WorkerPersistenceError: owner_link_closed\nAnother failure",
])
def test_inexact_resend_envelope_still_counts(home, output):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, last_output=output)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert not helpers._notices(conn)
