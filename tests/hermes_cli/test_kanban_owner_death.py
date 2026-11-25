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
from hermes_cli.kanban_worker_log import record_submitter_result

DEAD_OWNER = 43117
REFUSAL = "Kanban owner refused: Gateway inaccessible: service_identity_unverified"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    # All recorded processes are synthetic and dead, even if a number aliases pytest's PID.
    # The live-owner control opts into its real process explicitly below.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
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


def _claim(conn, tid, pid, board, owner_pid=DEAD_OWNER, birth=1.0, refused=True):
    """Claim ``tid`` for submitter ``pid`` (``tasks.worker_pid``) with a bound owner-side interpreter.

    ``refused``: the submitter exits on the owner's refusal, booking ``board["output"]`` through the
    production writer under its own pid, as ``kanban_worker_client._submit`` does. The shared task
    log (``_worker_final_output``) is never what a bound run's crash diagnosis quotes.
    """
    host = kb._claimer_id().split(":", 1)[0]
    claim_lock = f"{host}:w{pid}"
    kb.claim_task(conn, tid, claimer=claim_lock)
    run_id = _task(conn, tid).current_run_id
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?", (pid, int(time.time()) - 600, tid))
        kb._append_event(conn, tid, "owner_admitted",
                         {"db": str(board["owner_home"] / "state.db"), "session_id": "s", "request_id": f"r{pid}",
                          "pid": owner_pid, "birth": birth}, run_id=run_id)
        kb._append_event(conn, tid, "worker_bound",
                         {"pid": pid + 500000, "started_at": "x", "claim_lock": claim_lock}, run_id=run_id)
        if refused:
            assert record_submitter_result(conn, {"task_id": tid, "run_id": run_id, "claim_lock": claim_lock},
                                           pid, 1, board["output"]["text"])
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


@pytest.mark.parametrize("submitter_is_pytest", [False, True], ids=["fixed-pids", "pytest-pid"])
def test_owner_pid_gone_without_a_ledger_record_requeues_too(board, monkeypatch, submitter_is_pytest):
    with kbc.connect() as conn:
        # A CI worker can itself receive one of the fixture's fixed PID numbers.
        first = os.getpid() if submitter_is_pytest else 82001
        pids = [first, first + 1, first + 2]
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
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == me)
    worker_pid = me + 1  # distinct from the real owner, including on busy CI hosts
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), worker_pid, board,
                     owner_pid=me, birth=worker_birth(psutil.Process(me)))
        _reaped(monkeypatch, [worker_pid])
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert _task(conn, tid).consecutive_failures == 1
        assert "owner_restart_lost" not in _kinds(conn, tid)


def test_a_card_in_flight_at_two_consecutive_unclean_deaths_is_charged(isolate_usage_accounting, board, monkeypatch):
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


# --- the submitter's refusal is the run's receipt [daveey] ------------------------------------------------
# A bound managed run's crash diagnosis never quotes the shared task log (an older run's submitter can
# still flush into it), so the owner's refusal reaches the owner-death classifier only as a receipt the
# refused submitter itself books, keyed on its pid (``tasks.worker_pid``), the exact run and claim.

def _submitter_env(monkeypatch, tid, conn):
    task = _task(conn, tid)
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": tid,
                       "HERMES_KANBAN_RUN_ID": str(task.current_run_id), "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock,
                       "HERMES_KANBAN_DB": str(kb.kanban_db_path())}.items():
        monkeypatch.setenv(key, value)
    return task


def _refused_by_dead_owner(monkeypatch):
    from hermes_cli import kanban_worker_client
    from hermes_cli.gateway_client import GatewayClientError

    async def _refused(params, board_db):
        raise GatewayClientError("Gateway inaccessible: service_identity_unverified")
    monkeypatch.setattr(kanban_worker_client, "run", _refused)
    return kanban_worker_client


def _results(conn, tid):
    return [(r[0], json.loads(r[1])) for r in conn.execute(
        "SELECT run_id, payload FROM task_events WHERE task_id=? AND kind='worker_result' ORDER BY id", (tid,))]


def test_the_refused_submitter_books_its_own_run_qualified_receipt(board, monkeypatch, capsys):
    me = os.getpid()
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), me, board, refused=False)
        task = _submitter_env(monkeypatch, tid, conn)
    client = _refused_by_dead_owner(monkeypatch)

    assert client.main() == 1

    assert REFUSAL in capsys.readouterr().err
    with kbc.connect() as conn:
        assert _results(conn, tid) == [(task.current_run_id, {
            "pid": me, "claim_lock": task.claim_lock, "exit_code": 1, "last_output": REFUSAL, "submitter": True})]


def test_refusal_receipt_store_failure_preserves_exit_without_leaking_details(board, monkeypatch, capsys):
    from hermes_cli import kanban_worker_log
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), os.getpid(), board, refused=False)
        _submitter_env(monkeypatch, tid, conn)
    client = _refused_by_dead_owner(monkeypatch)

    def fail_store(*args):
        raise PermissionError("private database details")

    monkeypatch.setattr(kanban_worker_log, "record_submitter_result", fail_store)
    assert client.main() == 1
    stderr = capsys.readouterr().err
    assert REFUSAL in stderr
    assert "Kanban refusal receipt could not be recorded: PermissionError" in stderr
    assert "private database details" not in stderr
    assert stderr.rstrip().endswith(f"{KANBAN_WORKER_EXIT_TRAILER}1")
    with kbc.connect() as conn:
        assert _results(conn, tid) == []


def test_a_real_submitter_refusal_at_an_owner_death_requeues_uncounted_end_to_end(board, monkeypatch, capsys):
    """``main`` -> ``_submit`` -> the refusal receipt -> the dead-worker sweep -> ``owner_restart_lost``."""
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    _ledger(board, DEAD_OWNER)
    board["output"]["text"] = "an older run's reply flushed into the shared log"
    me = os.getpid()
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), me, board, refused=False)
        _submitter_env(monkeypatch, tid, conn)
    rc = _refused_by_dead_owner(monkeypatch).main()
    assert capsys.readouterr().err.rstrip().endswith(f"{KANBAN_WORKER_EXIT_TRAILER}{rc}")
    # ``_submit`` strips HERMES_KANBAN_* from its env; the sweep runs in the dispatcher's. The submitter is dead now.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    _reaped(monkeypatch, [me], rc)
    with kbc.connect() as conn:
        assert kbd.detect_crashed_workers(conn) == []
        task = _task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("ready", 0)
        payload = _last_payload(conn, tid, "owner_restart_lost")
        assert payload["owner_death_pid"] == DEAD_OWNER and payload["worker_output"] == REFUSAL
        error = conn.execute("SELECT error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                             (tid,)).fetchone()[0]
        assert "older run" not in error


def test_a_bound_run_with_no_receipt_does_not_trust_the_refusal_in_the_shared_log(board, monkeypatch):
    # Control: the same refusal only in the shared task log (any run's submitter may have flushed it)
    # proves nothing about this run, so the death is the card's crash, not the owner's.
    _ledger(board, DEAD_OWNER)
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), 88001, board, refused=False)
        _reaped(monkeypatch, [88001])
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert _task(conn, tid).consecutive_failures == 1
        assert "owner_restart_lost" not in _kinds(conn, tid)
        assert REFUSAL not in conn.execute("SELECT error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                                           (tid,)).fetchone()[0]
        assert not _last_payload(conn, tid, "crashed").get("worker_output")


def test_a_late_refusal_from_run_a_never_lands_on_run_b(isolate_usage_accounting, board, monkeypatch):
    _ledger(board, DEAD_OWNER)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="default")
        _claim(conn, tid, 89001, board, refused=False)
        a = _task(conn, tid)
        _reaped(monkeypatch, [89001])
        assert kbd.detect_crashed_workers(conn) == [tid]  # A dies on its own (no refusal booked)
        _claim(conn, tid, 89002, board, refused=False)
        b = _task(conn, tid)
        assert b.current_run_id != a.current_run_id
        a_params = {"task_id": tid, "run_id": a.current_run_id, "claim_lock": a.claim_lock}
        with kb.write_txn(conn):
            # A's submitter exits late, after B claimed: neither under A's run nor wearing B's run id or pid.
            assert not record_submitter_result(conn, a_params, 89001, 1, "OLD A: " + REFUSAL)
            assert not record_submitter_result(conn, dict(a_params, run_id=b.current_run_id), 89001, 1,
                                               "OLD A: " + REFUSAL)
            assert not record_submitter_result(conn, dict(a_params, run_id=b.current_run_id), 89002, 1,
                                               "OLD A: " + REFUSAL)
        assert _results(conn, tid) == []
        # B's own refusal is its receipt; A's text (also in the shared log) never reaches B's record.
        board["output"]["text"] = "OLD A: " + REFUSAL
        with kb.write_txn(conn):
            assert record_submitter_result(conn, {"task_id": tid, "run_id": b.current_run_id,
                                                  "claim_lock": b.claim_lock}, 89002, 1, REFUSAL)
        _reaped(monkeypatch, [89002])
        assert kbd.detect_crashed_workers(conn) == []
        assert _last_payload(conn, tid, "owner_restart_lost")["worker_output"] == REFUSAL
        error = conn.execute("SELECT error FROM task_runs WHERE id=?", (b.current_run_id,)).fetchone()[0]
        assert "OLD A" not in error
        assert _task(conn, tid).consecutive_failures == 1  # A's own crash only


def test_the_submitter_never_supersedes_the_interpreters_receipt(board, monkeypatch):
    # The interpreter booked a quota wall; the submitter's echo of the unknown receipt must not make it a crash.
    with kbc.connect() as conn:
        tid = _claim(conn, kb.create_task(conn, title="card", assignee="default"), 89101, board, refused=False)
        task = _task(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_result", {"pid": 89101 + 500000, "claim_lock": task.claim_lock,
                             "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE, "last_output": ""},
                             run_id=task.current_run_id)
            assert not record_submitter_result(conn, {"task_id": tid, "run_id": task.current_run_id,
                                                      "claim_lock": task.claim_lock}, 89101, 1, REFUSAL)
        _reaped(monkeypatch, [89101])
        assert kbd.detect_crashed_workers(conn) == []
        outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]
        assert outcome == "rate_limited"
