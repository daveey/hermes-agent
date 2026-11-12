"""Local failure receipts retain the captured run across competing observers."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_failure_transport as transport

live = transport.live


def _local_crash(live, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_owner_recovery as recovery
    tid, rid = transport._running(live)
    sync.set_mode(live, "off")
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET worker_pid=987654, claim_lock=?, started_at=1 WHERE id=?",
                     (kb._host_prefix() + "fixture", tid))
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
    monkeypatch.setattr(recovery, "managed_run_exit_code", lambda *a: 1)
    monkeypatch.setattr(recovery, "apply_attempt_note", lambda *a: None)
    monkeypatch.setattr(dispatch._host_reboot, "victims", lambda *a: (None, []))
    monkeypatch.setattr(dispatch._host_reboot, "note", lambda *a: None)
    monkeypatch.setattr(dispatch._owner_death, "note", lambda *a: None)
    monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                        dispatch._DeadWorker("crashed", 1, "fixture exit", "crashed", {}))
    return tid, rid


@pytest.mark.parametrize("limit", [1, 3])
def test_local_protocol_has_one_final_notice_without_early_charge(live, monkeypatch, limit):
    from hermes_cli.kanban_failure_notice import provisional_failure
    from types import SimpleNamespace
    tid, rid = _local_crash(live, monkeypatch)
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET max_retries=? WHERE id=?", (limit, tid))
    monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                        dispatch._DeadWorker("clean_exit", 0, "protocol violation", "protocol_violation",
                                             {"protocol_violation": True}, protocol_violation=True))
    assert dispatch.detect_crashed_workers(live) == [tid]
    events = [SimpleNamespace(kind=r["kind"], payload=json.loads(r["payload"])) for r in live.execute(
        "SELECT kind,payload FROM task_events WHERE task_id=? AND kind IN "
        "('protocol_violation','gave_up','failure_finalized')", (tid,))]
    assert [e.kind for e in events if not provisional_failure(e)] == ["failure_finalized"]
    assert kb.get_task(live, tid).consecutive_failures == (1 if limit == 1 else 0)
    dispatch._record_task_failure(live, tid, "late reclaim", outcome="reclaimed", run_id=rid)
    assert kb.get_task(live, tid).consecutive_failures == (1 if limit == 1 else 0)


def test_systemic_link_failure_emits_one_give_up_per_run(live, monkeypatch):
    from hermes_cli import kanban_systemic_crash as systemic
    tids = [_local_crash(live, monkeypatch)[0] for _ in range(3)]
    monkeypatch.setattr(systemic, "attach", lambda *a, **k: False)
    assert sorted(dispatch.detect_crashed_workers(live)) == sorted(tids)
    for tid in tids:
        rows = list(live.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)))
        assert len(rows) == 1
        assert json.loads(rows[0][0])["sticky"] is True
        assert json.loads(rows[0][0])["fix_card_link_failed"] is True
        assert kb.get_task(live, tid).consecutive_failures == 1


def test_local_crash_accounting_failure_rolls_back_reclaim(live, monkeypatch):
    tid, rid = _local_crash(live, monkeypatch)
    real = dispatch._record_task_failure

    def fail_after_accounting(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("accounting failed")

    monkeypatch.setattr(dispatch, "_record_task_failure", fail_after_accounting)
    with pytest.raises(RuntimeError, match="accounting failed"):
        dispatch.detect_crashed_workers(live)
    task = kb.get_task(live, tid)
    assert task.status == "running"
    assert task.current_run_id == rid
    assert task.consecutive_failures == 0
    assert live.execute("SELECT ended_at FROM task_runs WHERE id=?", (rid,)).fetchone()[0] is None
    assert not list(live.execute("SELECT kind FROM task_events WHERE task_id=? AND kind IN "
                                "('crashed','gave_up','failure_finalized')", (tid,)))
    monkeypatch.setattr(dispatch, "_record_task_failure", real)
    assert dispatch.detect_crashed_workers(live) == [tid]
    assert kb.get_task(live, tid).consecutive_failures == 1


def test_local_crash_sweep_suppresses_provisional_notice(live, monkeypatch):
    from hermes_cli.kanban_failure_notice import provisional_failure
    from types import SimpleNamespace
    tid, rid = _local_crash(live, monkeypatch)
    assert dispatch.detect_crashed_workers(live) == [tid]
    dispatch._record_task_failure(live, tid, "late reclaim", outcome="reclaimed", run_id=rid)
    assert kb.get_task(live, tid).consecutive_failures == 1
    events = [SimpleNamespace(kind=r["kind"], payload=json.loads(r["payload"])) for r in live.execute(
        "SELECT kind,payload FROM task_events WHERE task_id=? AND kind IN ('crashed','gave_up','failure_finalized')", (tid,))]
    assert [e.kind for e in events if not provisional_failure(e)] == ["failure_finalized"]


@pytest.mark.parametrize("limit", [1, 2])
def test_local_reclaim_and_crash_charge_captured_run_once(live, limit):
    from hermes_cli import kanban_db_sync as sync
    tid, rid = transport._running(live)
    sync.set_mode(live, "off")
    for outcome in ("reclaimed", "crashed"):
        dispatch._record_task_failure(live, tid, "exit", outcome=outcome, run_id=rid, failure_limit=limit)
    assert kb.get_task(live, tid).consecutive_failures == 1
    events = list(live.execute("SELECT kind, payload, run_id FROM task_events WHERE task_id=?", (tid,)))
    assert sum(e["kind"] == "gave_up" for e in events) == (1 if limit == 1 else 0)
    final = [e for e in events if e["kind"] == "failure_finalized"]
    assert len(final) == 1
    assert final[0]["run_id"] == rid
    assert json.loads(final[0]["payload"])["final_status"] == ("triage" if limit == 1 else "running")


def test_local_receipt_survives_counter_reset_and_event_prune(live):
    from hermes_cli import kanban_db_sync as sync
    tid, rid = transport._running(live)
    sync.set_mode(live, "off")
    dispatch._record_task_failure(live, tid, "exit", outcome="crashed", run_id=rid)
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET consecutive_failures=0 WHERE id=?", (tid,))
        live.execute("DELETE FROM task_events WHERE task_id=?", (tid,))
    dispatch._record_task_failure(live, tid, "late", outcome="reclaimed", run_id=rid)
    assert kb.get_task(live, tid).consecutive_failures == 0
    assert not list(live.execute("SELECT * FROM task_events WHERE task_id=?", (tid,)))


def test_local_late_old_run_cannot_charge_new_run(live):
    from hermes_cli import kanban_db_sync as sync
    tid, old = transport._running(live)
    sync.set_mode(live, "off")
    with kb.write_txn(live):
        new = live.execute("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES (?,'worker','running',2)", (tid,)).lastrowid
        live.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (new, tid))
    dispatch._record_task_failure(live, tid, "late", outcome="crashed", run_id=old)
    assert kb.get_task(live, tid).consecutive_failures == 0
    dispatch._record_task_failure(live, tid, "new exit", outcome="crashed", run_id=new)
    assert kb.get_task(live, tid).consecutive_failures == 1


def test_local_failure_receipt_rolls_back_with_counter_and_event(live):
    from hermes_cli import kanban_db_sync as sync
    tid, rid = transport._running(live)
    sync.set_mode(live, "off")
    with pytest.raises(RuntimeError, match="rollback"), kb.write_txn(live):
        dispatch._record_task_failure(live, tid, "exit", outcome="crashed", run_id=rid, allow_nested=True)
        raise RuntimeError("rollback")
    assert kb.get_task(live, tid).consecutive_failures == 0
    dispatch._record_task_failure(live, tid, "exit", outcome="crashed", run_id=rid)
    assert kb.get_task(live, tid).consecutive_failures == 1
    assert live.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='failure_finalized'", (tid,)).fetchone()[0] == 1


def test_stale_reclaim_cannot_end_replacement_run(live, monkeypatch):
    from hermes_cli import kanban_owner_recovery as recovery
    tid, old = _local_crash(live, monkeypatch)
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (tid,))
    monkeypatch.setattr(kb, "_stale_claim_kept", lambda *a, **kw: False)
    monkeypatch.setattr(recovery, "owner_reclaim_blocked_after_kill", lambda *a, **kw: False)
    replacement = []

    def replace_run(*args, **kwargs):
        with kb.write_txn(live):
            new = live.execute("INSERT INTO task_runs(task_id,profile,status,started_at) "
                               "VALUES (?,'worker','running',2)", (tid,)).lastrowid
            live.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (new, tid))
            replacement.append(new)
        return {}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", replace_run)
    assert kb.release_stale_claims(live) == 0
    task = kb.get_task(live, tid)
    assert task.status == "running"
    assert task.current_run_id == replacement[0] != old
    assert task.consecutive_failures == 0
    assert live.execute("SELECT ended_at FROM task_runs WHERE id=?", (replacement[0],)).fetchone()[0] is None


@pytest.mark.parametrize("delete", ["task_runs", "tasks"])
def test_unsynced_receipt_identity_and_cascade(tmp_path, monkeypatch, delete):
    from hermes_cli import kanban_db_connect as kbc
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, False)
    with kbc.connect(home / "kanban.db") as conn:
        assert "uid" not in {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
        tid = kb.create_task(conn, title="local receipt", assignee="worker")
        with kb.write_txn(conn):
            rid = conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at) "
                               "VALUES (?,'worker','running',1)", (tid,)).lastrowid
            conn.execute("UPDATE tasks SET current_run_id=?,status='running' WHERE id=?", (rid, tid))
        for outcome in ("reclaimed", "crashed"):
            dispatch._record_task_failure(conn, tid, "exit", outcome=outcome, run_id=rid, failure_limit=1)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        events = list(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='failure_finalized'", (tid,)))
        assert len(events) == 1
        assert json.loads(events[0][0])["run_uid"] == f"local:{rid}"
        with kb.write_txn(conn):
            if delete == "tasks":
                conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
            else:
                conn.execute("DELETE FROM task_runs WHERE id=?", (rid,))
        assert not list(conn.execute("SELECT * FROM kanban_local_failures"))


def test_local_foreign_run_cannot_charge_task(live):
    from hermes_cli import kanban_db_sync as sync
    tid, _ = transport._running(live)
    _, foreign = transport._running(live)
    sync.set_mode(live, "off")
    dispatch._record_task_failure(live, tid, "exit", outcome="crashed", run_id=foreign)
    assert kb.get_task(live, tid).consecutive_failures == 0
    assert not list(live.execute("SELECT * FROM task_events WHERE task_id=? AND kind='failure_finalized'", (tid,)))
