"""Cross-gateway card waits survive an unavailable SSH route without hiding broken reads."""
import json
import sqlite3
import subprocess
import time

import pytest

from hermes_cli import kanban_awaiting as awaiting
from hermes_cli import kanban_wait_check as wait
from hermes_cli import kanban_wake_check as wake

TID = "t_123456789abcdef0"


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path))
    db = sqlite3.connect(tmp_path / "hub.db")
    db.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
    scope = {"task_scopes": [{"board": "default", "profile": "default", "state": "ok", "observed_at": time.time()}]}
    db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", ("other", "gateway", "other", json.dumps(scope), int(time.time()*1000)))
    db.commit()
    monkeypatch.setattr(wait.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 255, "", "Could not resolve hostname"))
    yield db
    db.close()


def put(db, status, **fields):
    row = dict(id=TID, board="default", status=status, gateway="other", profile="default", owner_profile="default",
               wait_observation={"parked_agent": False, "orphan_reason": None}, **fields)
    db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", ("other", "tasks", f"default/default/{TID}", json.dumps(row), 1))
    db.commit()


@pytest.mark.parametrize("status,rc", [("done", 0), ("archived", 0), ("blocked", 0), ("running", 1), ("scheduled", 1)])
def test_fallback_status_including_terminal_records(replica, status, rc):
    put(replica, status)
    code, text = wait.check_remote("other", "task", TID)
    assert code == rc, text
    assert text.split()[:2] == [TID, status]


def parked_observation(events):
    """The staleness note ``confirm_remote`` stored, not the wake_check echo of an earlier read."""
    parks = [e.payload for e in events if e.kind == "scheduled" and isinstance(e.payload, dict)]
    record = parks[-1].get("awaiting")
    assert isinstance(record, dict)
    observed = record.get("observed")
    assert isinstance(observed, str)
    return observed


def pin_replica_clock(monkeypatch):
    """Freeze ``time.time`` so ``stale_age=`` is the age written, not age plus a deschedule.

    The reader reports live age (``time.time() - observed_at``, rounded). Hub CI freezes the
    process for a second or two between the write and the re-read in ``confirm_remote``;
    ``:.0f`` then prints 1239 for an age the test set to 1237. Pin before asserting the exact
    second. Monkeypatch restores the clock when the test ends.
    """
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    return now


def make_stale(db, age):
    scope = {"task_scopes": [{"profile": "default", "board": "default", "state": "ok", "observed_at": time.time() - age}]}
    db.execute("UPDATE replica SET data=? WHERE coll='gateway'", (json.dumps(scope),))
    db.commit()


def test_deschedule_changes_rounded_stale_age_unless_clock_is_pinned(replica, monkeypatch):
    # The CI miss: 2s of wall clock between the write and the read prints stale_age=1239s.
    # A pin keeps the exact second the assertion asks for.
    put(replica, "running")
    base = float(2**31)
    state = {"t": base}
    monkeypatch.setattr(time, "time", lambda: state["t"])
    make_stale(replica, 1237)
    state["t"] = base + 2
    assert "stale_age=1239s" in wait.check_remote("other", "task", TID)[1]
    state["t"] = base
    pin_replica_clock(monkeypatch)
    make_stale(replica, 1237)
    state["t"] = base + 2
    assert "stale_age=1237s" in wait.check_remote("other", "task", TID)[1]


def test_missing_is_error_and_stale_known_card_is_unknown_not_yet(replica, monkeypatch):
    assert wait.check_remote("other", "task", TID)[0] == 2
    put(replica, "done")
    pin_replica_clock(monkeypatch)
    make_stale(replica, 1237)
    code, text = wait.check_remote("other", "task", TID)
    # Seen in the last replica of an offline gateway: unknown, not yet (3), never "broken" (2) and never "met".
    assert code == 3, text
    assert text.split()[:2] == [TID, "done"] and "stale_age=1237s" in text and "gateway=other" in text
    assert not wake.output_error(text)
    # A missing card on a stale replica is still an error: there is nothing to have seen.
    replica.execute("DELETE FROM replica WHERE coll='tasks'")
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 2
    # A replica stamped far in the future is a broken clock, not an offline gateway.
    put(replica, "running")
    make_stale(replica, -3600)
    assert wait.check_remote("other", "task", TID)[0] == 2


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="waiter", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        yield conn, tid
    wake._shutdown_for_tests()


def _probe_results(conn, tid, cmd, rc, out, n, *, start=1000):
    for i in range(n):
        gen = wake.get_state(conn, tid)["generation"]
        wake.apply_result(conn, tid, cmd, rc, out, generation=gen, now=start + i * 900)


def test_stale_macbook_replica_is_not_marked_broken_after_three_checks(replica, board):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    put(replica, "running")
    make_stale(replica, 1237)
    rc, out = wait.check_remote("other", "task", TID)
    assert rc == 3
    cmd = f"hermes kanban wait-check task {TID} --gateway other --until done,blocked"
    assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", wake_check=cmd, check_every=900)
    _probe_results(conn, tid, cmd, rc, out, wake.BROKEN_AFTER + 2)
    assert kb.get_task(conn, tid).status == "scheduled"
    state = wake.get_state(conn, tid)
    assert state["fails"] == 0 and state["last_rc"] == 3
    assert not [e for e in kb.list_events(conn, tid) if e.kind == wake.BROKEN_EVENT]
    # Control: the old rc 2 "error: replica ..." still wakes as broken after BROKEN_AFTER checks.
    _probe_results(conn, tid, cmd, 2, "error: replica other: stale replica", wake.BROKEN_AFTER, start=9000)
    broken = [e for e in kb.list_events(conn, tid) if e.kind == wake.BROKEN_EVENT]
    assert broken and broken[-1].payload.get("reason") is None and kb.get_task(conn, tid).status == "ready"


def test_gateway_offline_past_cap_wakes_with_offline_note(replica, board):
    from hermes_cli import kanban_db as kb
    conn, tid = board
    put(replica, "running")
    make_stale(replica, wake.STALE_WAKE_AFTER + 2 * 3600)
    rc, out = wait.check_remote("other", "task", TID)
    cmd = f"hermes kanban wait-check task {TID} --gateway other"
    assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", wake_check=cmd, check_every=900)
    _probe_results(conn, tid, cmd, rc, out, 1)
    assert kb.get_task(conn, tid).status == "ready"
    ev = [e for e in kb.list_events(conn, tid) if e.kind == wake.BROKEN_EVENT][-1].payload
    assert ev["reason"] == "gateway_offline" and ev["gateway"] == "other"
    assert "Gateway other offline for 26 h" in ev["note"]
    assert "producer's gateway is offline" in wake.render_wake_context(conn, tid)


def test_schedule_awaiting_card_on_stale_replica_is_accepted(replica, board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    conn, tid = board
    pin_replica_clock(monkeypatch)
    put(replica, "running")
    make_stale(replica, 1237)
    rc, out = wait.check_remote("other", "task", TID)
    assert rc == 3
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))
    # The probe replays the stale reply (exit 3), so the park-time dry run sees exactly what wait-check prints.
    probe = f"printf '%s\\n' {json.dumps(out)}; exit 3"
    res = json.loads(kt._handle_schedule({
        "reason": f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on {TID} on other",
        "wake_check": probe, "check_every": 900,
        "awaiting": {"kind": "card", "ref": TID, "gateway": "other"}}))
    assert res.get("status") == "scheduled", res
    park = [e for e in kb.list_events(conn, tid) if e.kind == "scheduled"][-1]
    observed = parked_observation([park])
    assert "stale_age=1237s" in observed and f"{TID} running" in observed
    # An orphan seen in the stale replica is still refused.
    replica.execute("DELETE FROM replica WHERE coll='tasks'")
    put(replica, "archived")
    make_stale(replica, 1237)
    with pytest.raises(awaiting.AwaitingError):
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))


def _block_on_remote_card(conn, tid, monkeypatch):
    """The observed call: kanban_block(kind='time', awaiting={kind: card, gateway}) with no wake_check."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))
    return json.loads(kt._handle_block({
        "kind": "time", "reason": f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on {TID} on other",
        "awaiting": {"kind": "card", "ref": TID, "gateway": "other"}}))


def test_block_awaiting_card_on_stale_replica_is_accepted(replica, board, monkeypatch):
    # Regression: a stale replica (the producer's Mac asleep) refused the park with invalid_awaiting
    # "cannot confirm producer card ... stale replica"; it is unknown, not invalid.
    from hermes_cli import kanban_db as kb
    conn, tid = board
    awaiting._REMOTE_CONFIRMED.clear()
    pin_replica_clock(monkeypatch)
    put(replica, "running")
    make_stale(replica, 3176)
    res = _block_on_remote_card(conn, tid, monkeypatch)
    assert res.get("status") == "scheduled", res
    assert kb.get_task(conn, tid).status == "scheduled"
    observed = parked_observation(kb.list_events(conn, tid))
    assert "stale_age=3176s" in observed and f"{TID} running" in observed


@pytest.mark.parametrize("case", ["missing", "archived"])
def test_block_awaiting_card_on_stale_replica_refuses_missing_or_archived(replica, board, monkeypatch, case):
    # A stale replica that positively shows the card missing or archived is still refused.
    from hermes_cli import kanban_db as kb
    conn, tid = board
    awaiting._REMOTE_CONFIRMED.clear()
    if case == "archived":
        put(replica, "archived")
    make_stale(replica, 3176)
    res = _block_on_remote_card(conn, tid, monkeypatch)
    assert res.get("ok") is not True and "error" in json.dumps(res), res
    want = "invalid_awaiting" if case == "missing" else "agent_action_wait"
    assert want in json.dumps(res), res
    assert kb.get_task(conn, tid).status == "running"
    with pytest.raises(awaiting.AwaitingError) as exc:
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))
    assert exc.value.code == want


def test_since_and_until_preserved(replica):
    put(replica, "done")
    assert wait.check_remote("other", "task", TID, since=("done",))[0] == 1
    assert wait.check_remote("other", "task", TID, until=("blocked",))[0] == 1


def test_confirmation_accepts_live_but_rejects_orphan(replica):
    put(replica, "running")
    awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))
    replica.execute("DELETE FROM replica WHERE coll='tasks'")
    put(replica, "blocked", human_request=None, block_kind="transient")
    with pytest.raises(awaiting.AwaitingError):
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))


def test_handrolled_replica_probe_refused(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    cmd = f"hermes-hub tasks --gw other --json | python3 -c 'print(\"{TID}\")'"
    error = wake.dry_run_error(cmd)
    assert error and "hermes kanban wait-check task" in error


def test_qualified_wins_over_legacy_and_legacy_alone_is_unknown(replica):
    put(replica, "done")
    replica.execute("INSERT INTO replica VALUES(?,?,?,?,?)", ("other", "tasks", f"default/{TID}", json.dumps(dict(id=TID, board="default", status="running")), 1))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 0
    replica.execute("DELETE FROM replica WHERE key=?", (f"default/default/{TID}",))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 2


@pytest.mark.parametrize("failure", [OSError("no ssh"), subprocess.TimeoutExpired("ssh", 25)])
def test_transport_exceptions_fall_back(replica, monkeypatch, failure):
    put(replica, "done")
    def fail(*a, **kw):
        raise failure
    monkeypatch.setattr(wait.subprocess, "run", fail)
    assert wait.check_remote("other", "task", TID)[0] == 0


@pytest.mark.parametrize("status", ["archived", "triage"])
def test_non_live_confirmation(replica, status):
    put(replica, status)
    with pytest.raises(awaiting.AwaitingError):
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))


def test_scheduled_evidence_missing_is_error_and_orphan_wakes(replica):
    put(replica, "scheduled")
    row = json.loads(replica.execute("SELECT data FROM replica WHERE coll='tasks'").fetchone()[0])
    row.pop("wait_observation")
    replica.execute("UPDATE replica SET data=? WHERE coll='tasks'", (json.dumps(row),))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 2
    row["wait_observation"] = dict(parked_agent=True, orphan_reason="parked with no producer")
    replica.execute("UPDATE replica SET data=? WHERE coll='tasks'", (json.dumps(row),))
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 0
    with pytest.raises(awaiting.AwaitingError):
        awaiting.confirm_remote(dict(kind="card", ref=TID, gateway="other"))


def test_automatic_probe_since_status(replica, monkeypatch, capsys):
    from argparse import Namespace
    put(replica, "done")
    monkeypatch.setenv("HERMES_KANBAN_WAKE_PROBE", "1")
    monkeypatch.setattr(wait, "handled_status", lambda *a: "done")
    assert wait.run(Namespace(check="task", target=TID, gateway="other", board=None, until=None, since=None)) == 1
    assert "unchanged since park" in capsys.readouterr().out


def test_scope_and_identity_fail_closed(replica):
    put(replica, "done")
    assert wait.check_remote("other", "task", TID, board="elsewhere")[0] == 2
    assert wait.check_remote("elsewhere", "task", TID)[0] == 2
    replica.execute("DELETE FROM replica WHERE coll='gateway'")
    replica.commit()
    assert wait.check_remote("other", "task", TID)[0] == 2


def test_supported_probe_allowed(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    assert wake.dry_run_error(f"hermes kanban wait-check task {TID} --gateway other") is None


def test_owner_collector_publishes_wait_evidence(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_ledger import observe_owner_task
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect
    conn = connect()
    try:
        tid = kb.create_task(conn, title="collector fixture", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "scheduled", {"reason": "denied access; needs permission"})
        conn.execute("BEGIN")
        row = observe_owner_task(conn, dict(id=tid, status="scheduled", gateway="other", profile="default", board="default"), now=time.time())
        assert row["wait_observation"]["orphan_reason"]
        assert row["wait_observation"]["parked_agent"] is True
        conn.rollback()
        rc, out = wait.check_local("task", tid)
        assert "orphan producer" in out or "agent-owned park:" in out
        monkeypatch.setattr(wait.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, rc, out, ""))
        with pytest.raises(awaiting.AwaitingError):
            awaiting.confirm_remote(dict(kind="card", ref=tid, gateway="other"))
    finally:
        conn.close()
