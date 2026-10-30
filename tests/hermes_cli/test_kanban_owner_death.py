"""One unclean owner death must not park every in-flight Kanban card in sticky triage. [daveey]

2026-10-30 12:26 PDT, David-MBP: gateway 43117 segfaulted; its in-flight managed runs exited 1 with
"[owner admission settled interrupted] Kanban owner refused: Gateway inaccessible:
service_identity_unverified", three shared that fingerprint, and the systemic rule tripped them into
sticky triage with no owner (t_01a18db7ce134b33, t_01a18e08b1e764a1, t_01a18e122934644d).

* A run lost to its owner's death (owner-unavailable words + an unclean ledger record or a gone owner
  pid) is booked ``owner_restart_lost``, uncounted, however many die together.
* A card in flight at two consecutive unclean owner deaths is charged.
* A real systemic trip opens ONE fix card per fingerprint and the tripped cards wait on it.
"""

import json
import os
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_block_routing import has_sticky_block
from hermes_cli.kanban_systemic_crash import fix_key

DEAD_OWNER = 43117
REFUSAL = "Kanban owner refused: Gateway inaccessible: service_identity_unverified"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    me = os.getpid()
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == me)  # every worker and the old owner are dead
    output = {"text": REFUSAL}
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *a, **kw: output["text"])
    owner_home = tmp_path / "owner"
    (owner_home / "logs").mkdir(parents=True)
    return {"home": tmp_path, "owner_home": owner_home, "output": output}


def _ledger(board, *dead_pids):
    with (board["owner_home"] / "logs" / "gateway-exit-diag.log").open("a") as fh:
        for pid in dead_pids:
            fh.write(json.dumps({"ts": "2026-10-30T19:26:23Z", "tag": "gateway.previous_unclean_exit",
                                 "pid": pid + 1, "prior_pid": pid}) + "\n")


def _claim(conn, tid, pid, board, owner_pid=DEAD_OWNER, birth=1.0):
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    run_id = _task(conn, tid).current_run_id
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?", (pid, int(time.time()) - 600, tid))
        kb._append_event(conn, tid, "owner_admitted",
                         {"db": str(board["owner_home"] / "state.db"), "session_id": "s", "request_id": f"r{pid}",
                          "pid": owner_pid, "birth": birth}, run_id=run_id)
        kb._append_event(conn, tid, "worker_bound", {"pid": pid + 500000, "started_at": "x"}, run_id=run_id)
    return tid


def _reaped(monkeypatch, pids, code=1):
    monkeypatch.setattr(kbd, "_recent_worker_exits", {pid: ((code & 0xFF) << 8, time.time()) for pid in pids})


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def _last_payload(conn, tid, kind):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (tid, kind)).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def test_runs_lost_to_an_unclean_owner_death_requeue_uncounted(board, monkeypatch):
    _ledger(board, DEAD_OWNER)
    with kbc.connect() as conn:
        pids = [81001, 81002, 81003, 81004]
        tids = [_claim(conn, kb.create_task(conn, title=f"card {p}", assignee="default"), p, board) for p in pids]
        _reaped(monkeypatch, pids)
        assert kbd.detect_crashed_workers(conn) == []
        assert kbd.detect_crashed_workers._last_auto_blocked == []  # type: ignore[attr-defined]
        for tid in tids:
            task = _task(conn, tid)
            assert (task.status, task.consecutive_failures) == ("ready", 0)
            kinds = _kinds(conn, tid)
            assert "gave_up" not in kinds and "crashed" not in kinds
            payload = _last_payload(conn, tid, "owner_restart_lost")
            assert payload["owner_death_pid"] == DEAD_OWNER and payload["infrastructure"] is True
            assert payload["owner_exit"] == "unclean"
            outcome, error = conn.execute(
                "SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            assert outcome == "reclaimed" and f"pid {DEAD_OWNER}" in error
            assert kbd.check_respawn_guard(conn, tid) == "owner_restart_cooldown"
        assert not conn.execute("SELECT 1 FROM tasks WHERE idempotency_key LIKE 'systemic-crash:%'").fetchone()


def test_owner_pid_gone_without_a_ledger_record_requeues_too(board, monkeypatch):
    with kbc.connect() as conn:
        pids = [82001, 82002, 82003]
        tids = [_claim(conn, kb.create_task(conn, title=f"card {p}", assignee="default"), p, board) for p in pids]
        _reaped(monkeypatch, pids)
        assert kbd.detect_crashed_workers(conn) == []
        for tid in tids:
            assert (_task(conn, tid).status, _task(conn, tid).consecutive_failures) == ("ready", 0)
            assert _last_payload(conn, tid, "owner_restart_lost")["owner_exit"] == "pid_changed"


@pytest.mark.parametrize("reason, requeued", [("owner gone: gateway shutdown", True),
                                              ("managed_worker_failed", False)])
def test_a_worker_lost_note_counts_only_when_it_names_the_owner_going_away(board, monkeypatch, reason, requeued):
    board["output"]["text"] = ""
    _ledger(board, DEAD_OWNER)
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), 87001, board)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_lost", {"reason": reason, "bound": False},
                             run_id=_task(conn, tid).current_run_id)
        _reaped(monkeypatch, [87001])
        assert kbd.detect_crashed_workers(conn) == ([] if requeued else [tid])
        assert _task(conn, tid).consecutive_failures == (0 if requeued else 1)


def test_owner_still_alive_is_not_an_owner_death(board, monkeypatch):
    # Control: the admitting owner is this live process, so the refusal is not its death.
    from hermes_cli.process_identity import worker_birth
    me = os.getpid()
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), 83001, board,
                     owner_pid=me, birth=worker_birth(psutil.Process(me)))
        _reaped(monkeypatch, [83001])
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert _task(conn, tid).consecutive_failures == 1
        assert "owner_restart_lost" not in _kinds(conn, tid)


def test_a_card_in_flight_at_two_consecutive_unclean_deaths_is_charged(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "0")
    second_owner = 50001
    _ledger(board, DEAD_OWNER)
    with kbc.connect() as conn:
        tids = [kb.create_task(conn, title=f"card {i}", assignee="default") for i in range(3)]
        first = [84001, 84002, 84003]
        for tid, pid in zip(tids, first):
            _claim(conn, tid, pid, board)
        _reaped(monkeypatch, first)
        assert kbd.detect_crashed_workers(conn) == []
        # The next owner life dies uncleanly too, with the same cards in flight again.
        _ledger(board, second_owner)
        bystander = kb.create_task(conn, title="bystander", assignee="default")
        second = [85001, 85002, 85003, 85004]
        for tid, pid in zip(tids + [bystander], second):
            _claim(conn, tid, pid, board, owner_pid=second_owner)
        _reaped(monkeypatch, second)
        assert sorted(kbd.detect_crashed_workers(conn)) == sorted(tids)
        for tid in tids:
            task = _task(conn, tid)
            assert task.consecutive_failures == 1
            payload = _last_payload(conn, tid, "crashed")
            assert payload["owner_death_pid"] == second_owner and payload["owner_death_repeat"] == DEAD_OWNER
            # Charged normally, not as a systemic owner-less trip.
            assert task.status == "ready" and "gave_up" not in _kinds(conn, tid)
        # A card only in flight at the second death is still a bystander.
        assert (_task(conn, bystander).status, _task(conn, bystander).consecutive_failures) == ("ready", 0)


def test_a_systemic_crash_opens_one_fix_card_the_tripped_cards_wait_on(board, monkeypatch):
    board["output"]["text"] = "ImportError: cannot import name 'frobnicate' from 'hermes_cli.foo'"
    with kbc.connect() as conn:
        pids = [86001, 86002, 86003]
        tids = [_claim(conn, kb.create_task(conn, title=f"card {p}", assignee="default"), p, board) for p in pids]
        _reaped(monkeypatch, pids)
        assert sorted(kbd.detect_crashed_workers(conn)) == sorted(tids)
        fixes = conn.execute("SELECT id, idempotency_key FROM tasks WHERE idempotency_key LIKE 'systemic-crash:%'"
                             ).fetchall()
        assert len(fixes) == 1
        fix_id = fixes[0][0]
        for tid in tids:
            assert _task(conn, tid).status == "triage"
            assert _last_payload(conn, tid, "gave_up")["fix_card"] == fix_id
            assert not has_sticky_block(conn, tid)
            assert [p for p, _ in kb.parent_states(conn, tid)] == [fix_id]
        fix = _task(conn, fix_id)
        assert all(tid in (fix.body or "") for tid in tids)

        # Second tick, same fingerprint: no duplicate, the new cards wait on the same fix card.
        more_pids = [86004, 86005, 86006]
        more = [_claim(conn, kb.create_task(conn, title=f"card {p}", assignee="default"), p, board) for p in more_pids]
        _reaped(monkeypatch, more_pids)
        assert sorted(kbd.detect_crashed_workers(conn)) == sorted(more)
        assert conn.execute("SELECT count(*) FROM tasks WHERE idempotency_key LIKE 'systemic-crash:%'"
                            ).fetchone()[0] == 1
        for tid in more:
            assert _last_payload(conn, tid, "gave_up")["fix_card"] == fix_id
            assert [p for p, _ in kb.parent_states(conn, tid)] == [fix_id]
        assert any(all(t in c.body for t in more) for c in kb.list_comments(conn, fix_id))

        # The tripped cards hold while the fix card is open and resume once it is done.
        kb.recompute_ready(conn)
        assert _task(conn, tids[0]).status == "triage"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (fix_id,))
        kb.recompute_ready(conn)
        assert all(_task(conn, t).status == "ready" for t in tids + more)
        assert fixes[0][1] == fix_key(_fingerprint_of(conn, tids[0]))


def _fingerprint_of(conn, tid):
    return kbd._error_fingerprint(conn.execute(
        "SELECT error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])


def _task(conn, tid):
    task = kb.get_task(conn, tid)
    assert task is not None
    return task
