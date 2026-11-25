"""Selective rejection retirement uses authoritative evidence, never replica absence."""
import copy
import json
import math

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_fake_board as fake_board
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARD, ME, OTHER = "default", "macbook", "mini"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    client.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def reject_status(live, svc):
    tid = kb.create_task(live, title="contested", assignee="worker")
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    svc.board(BOARD)["leases"][tid] = {"task_id": tid, "holder": OTHER, "run_uid": "other-run", "expires": 9e15}
    sync.set_mode(live, "primary")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    client.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert client.status(live)["rejected"] == 1
    return tid


def test_normal_sync_retires_superseded_keeps_audit_and_is_idempotent(live, svc, kanban_home):
    reject_status(live, svc)
    # Every column but the local-only origin_gateway: the round pushes before it pulls (t_01a1d36ddcea59fe), so
    # the same round's pull lands the Board's echo of this audit row, which stamps that column.
    cols = [c for c in sync.table_columns(live, "task_events") if c != "origin_gateway"]
    audit_sql = f"SELECT {', '.join(cols)} FROM task_events WHERE kind='sync_rejected'"
    audits = [tuple(r) for r in live.execute(audit_sql)]
    assert audits
    result = client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert result.rejection_resolutions[0]["classification"] == "superseded"
    assert result.rejection_resolutions[0]["action"] == "retired"
    assert client.status(live)["rejected"] == 0
    assert [tuple(r) for r in live.execute(audit_sql)] == audits
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert client.diff(live, shadow, ME)["clean"]
    finally:
        shadow.close()


def test_matching_lagging_replica_cannot_retire_unresolved(live, svc, kanban_home):
    tid = reject_status(live, svc)
    client._pull_replica(BOARD, svc.hub(ME), ME)
    svc.board(BOARD)["rows"]["tasks"][tid]["status"] = "done"
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    result = resolve_rejections(live, svc.hub(ME), BOARD)
    assert result[0]["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert not client.diff(live, shadow, ME)["clean"]
    finally:
        shadow.close()


@pytest.mark.parametrize("veto", ["version", "pending", "identity", "untranslated_run", "local_change"])
def test_superseded_requires_settled_identity_and_local_snapshot(live, svc, monkeypatch, veto):
    tid = reject_status(live, svc)
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read(*args):
        evidence = original(*args)
        if veto == "version":
            evidence["row"]["version"] += 1
        elif veto == "identity":
            evidence["uid"] = "another-card"
        elif veto == "local_change":
            with kbc.write_txn(live):
                live.execute("UPDATE tasks SET title='changed' WHERE id=?", (tid,))
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    with kbc.write_txn(live):
        if veto == "pending":
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('pending','tasks',?,'upsert','{}','',2)", (tid,))
        elif veto == "untranslated_run":
            live.execute("UPDATE kanban_sync_state SET applying=1")
            live.execute("UPDATE tasks SET current_run_id=999999 WHERE id=?", (tid,))
            live.execute("UPDATE kanban_sync_state SET applying=0")
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = resolve_rejections(live, hub, BOARD)
    assert report[0]["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1


def test_cli_diff_dirty_then_clean_after_normal_sync(live, svc, kanban_home, monkeypatch, capsys):
    from argparse import Namespace
    from hermes_cli import kanban_sync_cli as cli
    reject_status(live, svc)
    monkeypatch.setattr(cli, "_board", lambda args: BOARD)
    monkeypatch.setattr(cli, "_path", lambda slug: kanban_home / "kanban.db")
    monkeypatch.setattr(client, "load_identity", lambda: svc.hub(ME).ident)
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "close", lambda: None, raising=False)
    monkeypatch.setattr(client, "Hub", lambda ident: hub)
    assert cli._diff(Namespace(json=True)) == 1
    assert json.loads(capsys.readouterr().out)["clean"] is False
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert cli._diff(Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["clean"] is True


@pytest.mark.parametrize("table,translated,settled", [
    ("task_events", False, True), ("task_events", True, True),
    ("task_runs", True, False), ("task_runs", True, True),
])
def test_child_reference_translation_and_run_version(live, svc, table, translated, settled):
    tid = kb.create_task(live, title="child controls", assignee="worker")
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        live.execute("INSERT INTO task_runs (id,uid,task_id,status,sync_version,started_at) VALUES (123,'run-uid',?,'done',?,1)",
                     (tid, 3 if settled else 2))
        if table == "task_events":
            live.execute("INSERT INTO task_events (uid,task_id,run_id,kind,created_at) VALUES ('event-uid',?,?,'done',1)",
                         (tid, 123 if translated else 999))
        live.execute("UPDATE kanban_sync_state SET applying=0")
        uid = "event-uid" if table == "task_events" else "run-uid"
        fields = {"run_uid": "old-run"} if table == "task_events" else {"status": "blocked"}
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state) "
                     "VALUES ('child-op',?,?,'upsert',?,'',1,'rejected')", (table, uid, json.dumps(fields)))
    remote = {"uid": uid, "version": 3, "status": "done", "run_uid": "run-uid" if translated else None}
    svc.board(BOARD)["rows"].setdefault(table, {})[uid] = remote
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    reports = resolve_rejections(live, svc.hub(ME), BOARD)
    expected = "superseded" if translated and settled else "unresolved"
    assert reports[0]["classification"] == expected
    assert client.status(live)["rejected"] == int(expected == "unresolved")


def ghost(live):
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error) "
                     "VALUES ('ghost-op','task_chat_links','ghost-link','upsert',?,'',1,'rejected','not_found')",
                     (json.dumps({"task_id": "t_ghost", "released": 1}),))


def test_unknown_task_link_retired_by_normal_sync(live, svc, kanban_home):
    ghost(live)
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert client.status(live)["rejected"] == 0


@pytest.mark.parametrize("offline", [False, True])
def test_cli_diff_does_not_report_clean_from_failed_refresh(live, svc, kanban_home, monkeypatch, capsys, offline):
    from argparse import Namespace
    from hermes_cli import kanban_sync_cli as cli
    client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    monkeypatch.setattr(cli, "_board", lambda args: BOARD)
    monkeypatch.setattr(cli, "_path", lambda slug: kanban_home / "kanban.db")
    monkeypatch.setattr(client, "load_identity", lambda: svc.hub(ME).ident)
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "close", lambda: None, raising=False)
    monkeypatch.setattr(client, "Hub", lambda ident: hub)
    svc.offline = offline
    rc = cli._diff(Namespace(json=True))
    report = json.loads(capsys.readouterr().out)
    assert report["clean"] is (not offline)
    assert rc == int(offline)
    assert bool(report.get("unavailable")) is offline


TASK, RUN = "t_released", "run-reclaimed"


def lost_release(live, svc, *, error="lease_lost: this run no longer holds the lease"):
    """worker-1 11-13 shape: the host's core-update reclaim ended the run on the Board ~3 s before the dead
    worker's own crash release was pushed, so that release was refused ``lease_lost`` and kept forever."""
    fields = {"failure_limit": 2, "force_trip": False, "infrastructure": False, "block_kind": "transient",
              "diagnostic": {"exit_kind": "signaled", "exit_code": 15}, "run_uid": RUN, "outcome": "crashed",
              "error": "pid 3264955 killed by signal 15"}
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error,"
                     "attempts) VALUES ('release-op','tasks',?,'release',?,'',1,'rejected',?,1)",
                     (TASK, json.dumps(fields), error))
    rows = svc.board(BOARD)["rows"]
    rows.setdefault("tasks", {})[TASK] = {"uid": TASK, "id": TASK, "status": "done", "version": 66, "created_on": OTHER,
                                          "current_run_uid": None, "lease_run_uid": None, "lease_holder": None}
    rows.setdefault("task_runs", {})[RUN] = {"uid": RUN, "task_id": TASK, "status": "reclaimed", "outcome": "reclaimed",
                                             "ended_at": 1794527437, "version": 4, "origin_gateway": ME}
    return rows


def test_lease_lost_release_of_ended_run_retired_by_normal_sync(live, svc, kanban_home):
    lost_release(live, svc)
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert client.diff(live, shadow, ME)["rejected_ops"] == 1
    finally:
        shadow.close()
    result = client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    report = result.rejection_resolutions[0]
    assert (report["classification"], report["action"]) == ("moot", "retired")
    assert report["run_evidence"]["row"]["ended_at"] == 1794527437
    assert client.status(live)["rejected"] == 0
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert client.diff(live, shadow, ME)["clean"]
    finally:
        shadow.close()


RELEASE_BOARD_VETOES = {
    "run_open": lambda rows: rows["task_runs"][RUN].update(ended_at=None),
    "lease_held": lambda rows: rows["tasks"][TASK].update(lease_run_uid=RUN),
    "current_run": lambda rows: rows["tasks"][TASK].update(current_run_uid=RUN),
    "other_task": lambda rows: rows["task_runs"][RUN].update(task_id="t_other"),
    "run_missing": lambda rows: rows["task_runs"].pop(RUN),
    "run_deleted": lambda rows: rows["task_runs"][RUN].update(deleted_at=1),
    "task_missing": lambda rows: rows["tasks"].pop(TASK),
}


@pytest.mark.parametrize("veto", [*RELEASE_BOARD_VETOES, "not_lease_lost", "local_run_open", "pending_run",
                                  "evidence_down"])
def test_release_retirement_needs_ended_run_off_the_lease(live, svc, monkeypatch, veto):
    rows = lost_release(live, svc, error="not_found: no such card" if veto == "not_lease_lost" else
                        "lease_lost: this run no longer holds the lease")
    RELEASE_BOARD_VETOES.get(veto, lambda rows: None)(rows)
    hub = svc.hub(ME)
    tid = None
    if veto in ("local_run_open", "pending_run"):
        tid = kb.create_task(live, title="local copy", assignee="worker")
    with kbc.write_txn(live):
        if veto in ("local_run_open", "pending_run"):
            live.execute("UPDATE kanban_sync_state SET applying=1")
            live.execute("INSERT INTO task_runs (id,uid,task_id,status,started_at) VALUES (77,?,?,'running',1)", (RUN, tid))
            live.execute("UPDATE kanban_sync_state SET applying=0")
        if veto == "pending_run":
            live.execute("UPDATE task_runs SET ended_at=2 WHERE uid=?", (RUN,))
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('run-pending','task_runs',?,'upsert','{}','',2)", (RUN,))
    if veto == "evidence_down":
        original = hub.row_evidence

        def read(board, table, uid):
            if table == "task_runs":
                raise OSError("board unreachable")
            return original(board, table, uid)
        monkeypatch.setattr(hub, "row_evidence", read)
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = next(r for r in resolve_rejections(live, hub, BOARD) if r["op_id"] == "release-op")
    assert (report["classification"], report["action"]) == ("unresolved", "retained")
    assert live.execute("SELECT state FROM kanban_sync_outbox WHERE op_id='release-op'").fetchone()[0] == "rejected"


def test_live_lease_by_the_run_keeps_release_end_to_end(live, kanban_home):
    """The ``lease_held`` veto through real Board transitions, not a hand-set row: the grant mirrors the lease
    onto the card (setLease), the holder then ends its run row and drops ``current_run_uid`` while keeping the
    lease, so only ``lease_run_uid`` still names the run. Once the holder leaves ``running`` (clearLease), the same
    release is moot. End-to-end on the Board side only: the rejected local release op is still inserted by hand."""
    svc = FakeService(lease_runs=True)
    hub = svc.hub(ME)
    tid = kb.create_task(live, title="leased", assignee="worker")
    client.push(live, hub, BOARD, ME, primary=False)
    granted = hub.leases(BOARD, {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "grant-op"})
    assert granted["status"] == "applied"
    card = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (card["lease_run_uid"], card["lease_holder"]) == (RUN, ME)

    def holder(op_id, table, uid, fields):
        res = hub.ops(BOARD, [{"client_op_id": op_id, "table": table, "uid": uid, "op": "upsert", "fields": fields,
                               "hlc": f"{9_999_999_999_999:013d}-0001-{ME}"}])["results"][0]
        assert res["status"] == "applied", res

    holder("run-end", "task_runs", RUN, {"ended_at": 1794527437, "status": "crashed", "outcome": "crashed"})
    holder("drop-current", "tasks", tid, {"current_run_uid": None})
    card = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (card["status"], card["current_run_uid"], card["lease_run_uid"]) == ("running", None, RUN)
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error,"
                     "attempts) VALUES ('release-op','tasks',?,'release',?,'',1,'rejected',?,1)",
                     (tid, json.dumps({"run_uid": RUN, "outcome": "crashed"}),
                      "lease_lost: this run no longer holds the lease"))
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = next(r for r in resolve_rejections(live, hub, BOARD) if r["op_id"] == "release-op")
    assert (report["classification"], report["action"]) == ("unresolved", "retained")
    assert report["evidence"]["row"]["lease_run_uid"] == RUN
    assert client.status(live)["rejected"] == 1

    holder("leave-running", "tasks", tid, {"status": "blocked"})
    assert svc.board(BOARD)["rows"]["tasks"][tid]["lease_run_uid"] is None
    report = next(r for r in resolve_rejections(live, hub, BOARD) if r["op_id"] == "release-op")
    assert (report["classification"], report["action"]) == ("moot", "retired")
    assert client.status(live)["rejected"] == 0


def test_fake_lease_release_after_run_end_clears_mirror(live, kanban_home):
    """As releaseTask's clearLease: releasing a lease whose run row already ended still drops lease_run_uid."""
    svc = FakeService(lease_runs=True)
    hub = svc.hub(ME)
    tid = kb.create_task(live, title="leased", assignee="worker")
    client.push(live, hub, BOARD, ME, primary=False)
    assert hub.leases(BOARD, {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "g"})["status"] == "applied"
    res = hub.ops(BOARD, [{"client_op_id": "end", "table": "task_runs", "uid": RUN, "op": "upsert",
                           "fields": {"ended_at": 1794527437, "status": "crashed", "outcome": "crashed"},
                           "hlc": f"{9_999_999_999_999:013d}-0001-{ME}"}])["results"][0]
    assert res["status"] == "applied", res
    assert hub.leases(BOARD, {"action": "release", "task": tid, "run_uid": RUN, "client_op_id": "r"})["status"] == "applied"
    card = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (card["lease_holder"], card["lease_run_uid"], card["lease_expires"]) == (None, None, None)
    # releaseTask also moves the still-running card to retryStatusForRun and endRun clears current_run_uid.
    assert (card["status"], card["current_run_uid"]) == ("ready", None)
    assert svc.board(BOARD)["rows"]["task_runs"][RUN]["outcome"] == "crashed"  # the holder's own end stands


def test_fake_lease_release_after_review_run_end_retries_to_review(live, kanban_home):
    """retryStatusForRun: a run claimed out of review goes back to ``review`` when its ended lease is released."""
    svc = FakeService(lease_runs=True)
    hub = svc.hub(ME)
    tid = kb.create_task(live, title="leased", assignee="worker")
    client.push(live, hub, BOARD, ME, primary=False)
    grant = {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "g", "lane": "review"}
    assert hub.leases(BOARD, grant)["status"] == "applied"
    hub.ops(BOARD, [{"client_op_id": "end", "table": "task_runs", "uid": RUN, "op": "upsert",
                     "fields": {"ended_at": 1794527437, "status": "crashed", "outcome": "crashed"},
                     "hlc": f"{9_999_999_999_999:013d}-0001-{ME}"}])
    assert hub.leases(BOARD, {"action": "release", "task": tid, "run_uid": RUN, "client_op_id": "r"})["status"] == "applied"
    card = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (card["status"], card["current_run_uid"], card["lease_run_uid"]) == ("review", None, None)


@pytest.mark.parametrize("gateway,run_uid", [(ME, "run-someone-else"), (OTHER, RUN)])
def test_fake_lease_release_is_fenced_on_the_holders_run(live, kanban_home, gateway, run_uid):
    """As releaseTask's fence(): a release naming another run, or from another gateway, is lease_lost and leaves the
    lease, its card mirror and the run untouched."""
    svc = FakeService(lease_runs=True)
    hub = svc.hub(ME)
    tid = kb.create_task(live, title="leased", assignee="worker")
    client.push(live, hub, BOARD, ME, primary=False)
    assert hub.leases(BOARD, {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "g"})["status"] == "applied"
    res = svc.hub(gateway).leases(BOARD, {"action": "release", "task": tid, "run_uid": run_uid, "client_op_id": "r"})
    assert (res["status"], res["reason"]) == ("rejected", "lease_lost")
    b = svc.board(BOARD)
    card = b["rows"]["tasks"][tid]
    assert b["leases"][tid]["run_uid"] == RUN
    assert (card["status"], card["current_run_uid"], card["lease_holder"], card["lease_run_uid"]) == (
        "running", RUN, ME, RUN)
    assert b["rows"]["task_runs"][RUN]["ended_at"] is None


@pytest.mark.parametrize("extra,lane,run_outcome,kind,payload,status", [
    # An ordinary release (no unused_grant) still emits its run-outcome event with retry_status.
    ({}, "ready", "released", "released", {"error": None, "retry_status": "ready"}, "ready"),
    ({"unused_grant": True}, "ready", "released", "released",
     {"error": None, "retry_status": "ready", "unused_grant": True}, "ready"),
    # retryStatusForRun: a run claimed out of review retries to review.
    ({}, "review", "released", "released", {"error": None, "retry_status": "review"}, "review"),
    # NEUTRAL_RELEASES maps the outcome to its run outcome and event kind.
    ({"outcome": "owner_restart", "error": "gateway restarted"}, "ready", "reclaimed", "owner_restart_lost",
     {"owner_restart": True, "error": "gateway restarted", "retry_status": "ready"}, "ready"),
    ({"outcome": "rate_limited"}, "ready", "rate_limited", "rate_limited",
     {"error": "rate_limited", "retry_status": "ready"}, "ready"),
    ({"outcome": "terminal_provider", "error": "bad key"}, "ready", "crashed", "crashed",
     {"failure_notice": "canonical", "error": "bad key", "retry_status": "ready", "terminal_provider": True}, "ready"),
    # The holder's diagnostic belongs to a canonical failure: merged into run metadata and the event payload.
    ({"outcome": "timed_out", "diagnostic": {"exit_code": 124}}, "ready", "timed_out", "timed_out",
     {"exit_code": 124, "failure_notice": "canonical", "error": "timed_out", "retry_status": "ready"}, "ready"),
    ({"outcome": "crashed", "diagnostic": {"tail": "x" * 9000}}, "ready", "crashed", "crashed",
     {"diagnostic_truncated": True, "failure_notice": "canonical", "error": "crashed", "retry_status": "ready"},
     "ready"),
    # ...but not to a plain release; a neutral release merges the holder's extra instead.
    ({"diagnostic": {"exit_code": 0}}, "ready", "released", "released", {"error": None, "retry_status": "ready"},
     "ready"),
    ({"outcome": "owner_down", "extra": {"host": "mini"}}, "ready", "owner_down", "owner_down",
     {"host": "mini", "error": "owner_down", "retry_status": "ready"}, "ready"),
])
def test_fake_lease_release_emits_run_outcome_event(live, kanban_home, extra, lane, run_outcome, kind, payload,
                                                    status):
    """As transitions.ts releaseTask: every release of the holder's live run emits its run-outcome event carrying
    ``retry_status`` (``unused_grant`` only when set), with neutral outcomes mapped through NEUTRAL_RELEASES."""
    svc = FakeService(lease_runs=True)
    hub = svc.hub(ME)
    tid = kb.create_task(live, title="leased", assignee="worker")
    client.push(live, hub, BOARD, ME, primary=False)
    grant = {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "g", "lane": lane}
    assert hub.leases(BOARD, grant)["status"] == "applied"
    release = {"action": "release", "task": tid, "run_uid": RUN, "client_op_id": "r", **extra}
    assert hub.leases(BOARD, release)["status"] == "applied"
    rows = svc.board(BOARD)["rows"]
    events = [e for e in rows["task_events"].values() if e["run_uid"] == RUN and e["kind"] != "claimed"]
    assert [(e["kind"], json.loads(e["payload"])) for e in events] == [(kind, payload)]
    assert (rows["task_runs"][RUN]["outcome"], rows["tasks"][tid]["status"]) == (run_outcome, status)
    assert rows["tasks"][tid]["lease_run_uid"] is None


@pytest.fixture(scope="module")
def _task_seed(tmp_path_factory):
    """Exercise create/push once; release matrices test the fake, not DB migration.

    Never share a connection or mutable Board with a test. Integration cases still
    use fresh live databases and the real sync path per case.
    """
    with pytest.MonkeyPatch.context() as mp:
        home = support.kanban_home(tmp_path_factory.mktemp("release-seed"), mp)
        support.write_sync_config(home, True)
        conn = kbc.connect(home / "kanban.db")
        try:
            client.ensure_identity(conn, BOARD, ME)
            svc = FakeService()
            tid = kb.create_task(conn, title="leased", assignee="worker")
            client.push(conn, svc.hub(ME), BOARD, ME, primary=False)
            return tid, copy.deepcopy(svc.board(BOARD))
        finally:
            conn.close()


@pytest.fixture
def task_seed(_task_seed):
    return copy.deepcopy(_task_seed)


def _leased(task_seed, svc):
    tid, board = task_seed
    svc.boards[BOARD] = board
    hub = svc.hub(ME)
    grant = {"action": "grant", "task": tid, "run_uid": RUN, "client_op_id": "g"}
    assert hub.leases(BOARD, grant)["status"] == "applied"
    return hub, tid


@pytest.mark.parametrize("lease_runs", [False, True])
def test_release_seed_isolated_from_template(task_seed, _task_seed, lease_runs):
    tid, pristine = _task_seed
    before = copy.deepcopy(pristine)
    svc = FakeService(lease_runs=lease_runs)
    hub, copied_tid = _leased(task_seed, svc)
    assert copied_tid == tid
    assert svc.board(BOARD) is not pristine
    assert pristine == before
    assert not pristine["leases"]
    assert not pristine["rows"].get("task_runs")
    assert _release("leases", svc, hub, tid, outcome="crashed")["status"] == "applied"
    svc.board(BOARD)["rows"]["tasks"][tid]["title"] = "mutated copy"
    svc.board(BOARD)["changes"].clear()
    assert pristine == before


def test_fake_lease_release_merges_diagnostic_into_run_metadata(task_seed):
    """As releaseTask: a crashed release's diagnostic and protocol_violation flag land in the run metadata."""
    svc = FakeService(lease_runs=True)
    hub, tid = _leased(task_seed, svc)
    release = {"action": "release", "task": tid, "run_uid": RUN, "client_op_id": "r", "outcome": "crashed",
               "diagnostic": {"exit_code": 1}, "protocol_violation": True}
    assert hub.leases(BOARD, release)["status"] == "applied"
    run = svc.board(BOARD)["rows"]["task_runs"][RUN]
    assert json.loads(run["metadata"]) == {"exit_code": 1, "protocol_violation": True}


@pytest.mark.parametrize("lease_runs", [True, False])
def test_fake_lease_release_rejects_unknown_outcome(task_seed, lease_runs):
    """As releaseTask's RELEASE_OUTCOMES guard: a typo'd outcome is ``invalid`` and changes nothing."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    release = {"action": "release", "task": tid, "run_uid": RUN, "client_op_id": "r", "outcome": "crashd"}
    res = hub.leases(BOARD, release)
    assert (res["status"], res["reason"]) == ("rejected", "invalid")
    assert svc.board(BOARD) == before
    b = svc.board(BOARD)
    op = {"op": "release", "task": tid, "run_uid": RUN, "client_op_id": "o", "outcome": "crashd"}
    assert svc.release_op(b, op, ME) == {"status": "rejected", "reason": "invalid"}
    assert svc.board(BOARD) == before


def _release(route, svc, hub, tid, **fields):
    """Release RUN through the leases route or the ops route (both are transitions.ts releaseTask)."""
    if route == "leases":
        return hub.leases(BOARD, {"action": "release", "task": tid, "run_uid": RUN, "client_op_id": "r", **fields})
    return svc.release_op(svc.board(BOARD), {"op": "release", "task": tid, "run_uid": RUN, "client_op_id": "r",
                                             **fields}, ME)


def _outcome_event(svc, kind):
    [event] = [e for e in svc.board(BOARD)["rows"]["task_events"].values()
               if e["run_uid"] == RUN and e["kind"] == kind]
    return json.loads(event["payload"])


@pytest.mark.parametrize("route", ["leases", "ops"])
@pytest.mark.parametrize("tail,truncated", [
    # JSON.stringify counts UTF-16 code units with no \uXXXX escaping: 3000 "é" is 3011 chars, kept (ASCII
    # escaping would count 18011 and truncate)...
    ("é" * 3000, False),
    # ...and an astral character is two code units: 3995 emoji is 8001 chars, truncated (code points: 4006).
    ("\U0001F600" * 3995, True),
    ("x" * 7989, False),  # {"tail":"..."} is exactly MAX_DIAGNOSTIC_CHARS: kept.
    ("x" * 7990, True),
], ids=["e-acute-3000-kept", "emoji-3995-truncated", "ascii-at-limit-kept", "ascii-over-limit-truncated"])
def test_fake_release_truncates_diagnostic_like_json_stringify(task_seed, route, tail, truncated):
    """As releaseDiagnostic on both routes: ``JSON.stringify(value).length > MAX_DIAGNOSTIC_CHARS`` replaces the
    diagnostic by ``{diagnostic_truncated: true}`` in run metadata and the event, with a protocol_violation crash
    also marked in run metadata."""
    svc = FakeService(lease_runs=True)
    hub, tid = _leased(task_seed, svc)
    res = _release(route, svc, hub, tid, outcome="crashed", diagnostic={"tail": tail}, protocol_violation=True)
    assert res["status"] == "applied"
    evidence = {"diagnostic_truncated": True} if truncated else {"tail": tail}
    run = svc.board(BOARD)["rows"]["task_runs"][RUN]
    assert json.loads(run["metadata"]) == {**evidence, "protocol_violation": True}
    payload = _outcome_event(svc, "crashed")
    assert {k: payload.get(k) for k in evidence} == evidence
    assert "protocol_violation" not in payload


@pytest.mark.parametrize("route", ["leases", "ops"])
def test_fake_release_neutral_merges_extra_on_both_routes(task_seed, route):
    """As releaseTask's NEUTRAL_RELEASES branch on both routes: the holder's ``extra`` (+ ``owner_restart``) lands
    in run metadata and the event; a diagnostic does not."""
    svc = FakeService(lease_runs=True)
    hub, tid = _leased(task_seed, svc)
    res = _release(route, svc, hub, tid, outcome="owner_restart", extra={"host": "mini"}, diagnostic={"rc": 1})
    assert res["status"] == "applied"
    assert json.loads(svc.board(BOARD)["rows"]["task_runs"][RUN]["metadata"]) == {"host": "mini",
                                                                                   "owner_restart": True}
    payload = _outcome_event(svc, "owner_restart_lost")
    assert (payload["host"], payload["owner_restart"], "rc" in payload) == ("mini", True, False)


@pytest.mark.parametrize("route,lease_runs", [("leases", True), ("leases", False), ("ops", True)])
@pytest.mark.parametrize("outcome", ["released", "crashed", "timed_out", "owner_down"])
@pytest.mark.parametrize("diagnostic", ["boom", [["exit_code", 1]], [], "", 0, False])
def test_fake_release_rejects_non_object_diagnostic(task_seed, route, lease_runs, outcome, diagnostic):
    """As releaseDiagnostic: a diagnostic that is not an object is ``invalid`` for every outcome (not only the
    canonical failures that use it) and changes nothing."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    res = _release(route, svc, hub, tid, outcome=outcome, diagnostic=diagnostic)
    assert (res["status"], res["reason"]) == ("rejected", "invalid")
    assert svc.board(BOARD) == before


@pytest.mark.parametrize("route,lease_runs", [("leases", True), ("leases", False), ("ops", True)])
@pytest.mark.parametrize("outcome", ["released", "crashed", "timed_out", "owner_down"])
@pytest.mark.parametrize("extra", [[], "", 0, False, "host", [["host", "mini"]]])
def test_fake_release_rejects_non_object_extra(task_seed, route, lease_runs, outcome, extra):
    """As releaseTask: ``op.extra ?? {}`` that is not an object is ``invalid`` ("failure extra must be an object")
    for every outcome, before releaseDiagnostic, and changes nothing."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    res = _release(route, svc, hub, tid, outcome=outcome, extra=extra, diagnostic={"rc": 1})
    assert (res["status"], res["reason"]) == ("rejected", "invalid")
    assert svc.board(BOARD) == before


@pytest.mark.parametrize("route", ["leases", "ops"])
def test_fake_release_null_extra_is_empty(task_seed, route):
    """``extra: null`` is ``op.extra ?? {}`` = ``{}``: a neutral release applies with no holder metadata."""
    svc = FakeService(lease_runs=True)
    hub, tid = _leased(task_seed, svc)
    assert _release(route, svc, hub, tid, outcome="owner_down", extra=None)["status"] == "applied"
    assert svc.board(BOARD)["rows"]["task_runs"][RUN]["metadata"] is None


@pytest.mark.parametrize("number,literal", [
    (1.0, "1"), (-0.0, "0"), (123.456, "123.456"), (1e16, "10000000000000000"), (1e20, "100000000000000000000"),
    (1e21, "1e+21"), (1.5e300, "1.5e+300"), (1e-7, "1e-7"), (1.25e-7, "1.25e-7"), (0.00001, "0.00001"),
    (0.000001, "0.000001"), (float("nan"), "null"), (float("inf"), "null"), (float("-inf"), "null"),
    (10**21, "1e+21"), (2**60, "1152921504606847000"), (10**400, "null"), (True, "true"), (None, "null"),
    ([1.0, float("nan"), "é"], '[1,null,"é"]'), ({"k": 2.50}, '{"k":2.5}'),
], ids=lambda v: repr(v)[:24])
def test_fake_release_diagnostic_counts_numbers_like_json_stringify(number, literal):
    """releaseDiagnostic measures ``JSON.stringify(value).length``: a number in the diagnostic counts as JS writes
    it (1.0 -> ``1``, 1e-7 -> ``1e-7``, NaN/Infinity -> ``null``), not as Python's ``json.dumps``."""
    frame = '{"v":' + literal + ',"tail":""}'
    fill = FakeService.MAX_DIAGNOSTIC_CHARS - len(frame)
    kept = FakeService.release_diagnostic({"v": number, "tail": "x" * fill})
    assert kept is not None and "diagnostic_truncated" not in kept
    assert FakeService.release_diagnostic({"v": number, "tail": "x" * (fill + 1)}) == {"diagnostic_truncated": True}


@pytest.mark.parametrize("route,lease_runs", [("leases", True), ("leases", False), ("ops", True)])
def test_fake_release_spawn_failed_is_not_modelled(task_seed, route, lease_runs):
    """releaseTask hands spawn_failed to endCustody + recordTaskFailure; the fake refuses it loudly on every
    route and changes nothing."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    with pytest.raises(NotImplementedError):
        _release(route, svc, hub, tid, outcome="spawn_failed")
    assert svc.board(BOARD) == before
    assert svc.board(BOARD)["leases"][tid]["run_uid"] == RUN


@pytest.mark.parametrize("route", ["leases", "ops"])
@pytest.mark.parametrize("fence_arm", ["missing", "other_holder", "stale"])
@pytest.mark.parametrize("bad", [{}, {"failure_limit": 0}, {"block_kind": "blocked"}, {"extra": "host"}],
                         ids=["valid", "failure_limit", "block_kind", "extra"])
def test_fake_release_spawn_failed_is_fenced_first(task_seed, route, fence_arm, bad):
    """releaseTask runs fence() before its input checks and the spawn_failed branch: with no lease
    (``not lease``), another gateway's lease (``holder != gw``) or a newer run's lease, a spawn_failed release is
    ``lease_lost`` (not ``invalid`` for a bad input, nor the fake's NotImplementedError) and changes nothing."""
    svc = FakeService(lease_runs=True)
    hub, tid = _leased(task_seed, svc)
    leases = svc.board(BOARD)["leases"]
    if fence_arm == "missing":
        del leases[tid]
    else:
        leases[tid].update({"holder": OTHER} if fence_arm == "other_holder" else {"run_uid": "run-newer"})
    before = copy.deepcopy(svc.board(BOARD))
    res = _release(route, svc, hub, tid, outcome="spawn_failed", **bad)
    assert (res["status"], res["reason"]) == ("rejected", "lease_lost")
    assert svc.board(BOARD) == before


@pytest.mark.parametrize("route,lease_runs", [("leases", True), ("leases", False), ("ops", True)])
@pytest.mark.parametrize("bad", [{"extra": "host"}, {"diagnostic": "boom"}])
def test_fake_release_spawn_failed_validates_inputs_first(task_seed, route, lease_runs, bad):
    """releaseTask validates extra and the diagnostic before the spawn_failed branch: a bad one is ``invalid``
    and changes nothing."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    res = _release(route, svc, hub, tid, outcome="spawn_failed", **bad)
    assert (res["status"], res["reason"]) == ("rejected", "invalid")
    assert svc.board(BOARD) == before


@pytest.mark.parametrize("route,lease_runs", [("leases", True), ("leases", False), ("ops", True)])
@pytest.mark.parametrize("outcome", ["released", "crashed", "timed_out", "owner_down", "spawn_failed"])
@pytest.mark.parametrize("bad", [
    # failure_limit: Number(value) must be a positive safe integer when present (an explicit null is 0).
    {"failure_limit": 0}, {"failure_limit": -1}, {"failure_limit": 1.5}, {"failure_limit": None},
    {"failure_limit": "abc"}, {"failure_limit": 2**53}, {"failure_limit": float("nan")},
    {"failure_limit": float("inf")}, {"failure_limit": {}},
    # block_kind: String(op.block_kind ?? "transient") must be transient | assignment | capability.
    {"block_kind": "blocked"}, {"block_kind": ""}, {"block_kind": "Transient"}, {"block_kind": 0},
    {"block_kind": False},
], ids=lambda v: repr(v)[:32])
def test_fake_release_rejects_bad_failure_policy(task_seed, route, lease_runs, outcome, bad):
    """As releaseTask's post-fence policy checks (before extra and the diagnostic): a bad ``failure_limit`` or
    ``block_kind`` is ``invalid`` for every outcome, spawn_failed included (not the fake's NotImplementedError),
    and changes nothing."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    res = _release(route, svc, hub, tid, outcome=outcome, **bad)
    assert (res["status"], res["reason"]) == ("rejected", "invalid")
    assert svc.board(BOARD) == before


@pytest.mark.parametrize("policy", [
    {}, {"failure_limit": 1}, {"failure_limit": 3.0}, {"failure_limit": "3"}, {"failure_limit": " 2 "},
    {"failure_limit": True}, {"failure_limit": 2**53 - 1}, {"block_kind": None}, {"block_kind": "transient"},
    {"block_kind": "assignment"}, {"block_kind": "capability"},
], ids=lambda v: repr(v)[:32])
def test_fake_release_accepts_valid_failure_policy(policy):
    """What releaseTask accepts: ``Number(failure_limit)`` a positive safe integer (JS coerces ``"3"`` and
    ``true``), ``block_kind`` absent, null (``?? "transient"``) or one of the three kinds."""
    assert FakeService.release_inputs(policy) == {}


@pytest.mark.parametrize("route,lease_runs", [("leases", True), ("leases", False), ("ops", True)])
@pytest.mark.parametrize("outcome", ["released", "crashed", "owner_down", "spawn_failed"])
@pytest.mark.parametrize("run_uid", ["absent", None])
def test_fake_release_without_run_uid_is_invalid(task_seed, route, lease_runs, outcome, run_uid):
    """releaseTask refuses a release with no (or a null) ``run_uid`` ``invalid`` before its fence (so not
    ``lease_lost``, nor a KeyError or NotImplementedError in the fake), and nothing changes."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    body = {"task": tid, "client_op_id": "r", "outcome": outcome,
            **({} if run_uid == "absent" else {"run_uid": run_uid})}
    res = (hub.leases(BOARD, {"action": "release", **body}) if route == "leases"
           else svc.release_op(svc.board(BOARD), {"op": "release", **body}, ME))
    assert (res["status"], res["reason"]) == ("rejected", "invalid")
    assert svc.board(BOARD) == before


# Each expected value is what node prints for ``Number(value)`` (checked on node 22; the review checked node 26).
@pytest.mark.parametrize("value,expected", [
    # ASCII digits only: U+0663 (ARABIC-INDIC DIGIT THREE) is a Unicode \d and float() reads it; JS: NaN.
    ("\u0663", math.nan), ("\u06632", math.nan), ("1_000", math.nan),
    # StrWhiteSpaceChar, not str.strip(): U+001C-U+001F and U+0085 are not trimmed, U+FEFF is; Zs, LS and PS are.
    ("\x1c3\x1c", math.nan), ("\x1f3", math.nan), ("\x853", math.nan), ("\u180e3", math.nan), ("3\u200b", math.nan),
    ("\ufeff3\ufeff", 3.0), ("\u00a03\u3000", 3.0), ("\u20283\u2029", 3.0), (" \t\n\v\f\r3 ", 3.0),
    ("\u16803", 3.0), ("\u202f3\u205f", 3.0),
    # Unsigned non-decimal literals.
    ("0x10", 16.0), ("0X1f", 31.0), ("0o7", 7.0), ("0b11", 3.0), ("-0x10", math.nan), ("0x", math.nan),
    ("0xg", math.nan), ("0x" + "f" * 300, math.inf),
    ("1e3", 1000.0), (".5e1", 5.0), ("5.", 5.0), ("+Infinity", math.inf), ("-Infinity", -math.inf),
    ("infinity", math.nan), ("inf", math.nan), ("nan", math.nan), ("", 0.0), ("  ", 0.0),
    # Arrays go through String(): [] -> "" -> 0, [x] -> Number(String(x)), [x, y] -> "x,y" -> NaN.
    ([], 0.0), ([3], 3.0), (["3"], 3.0), ([None], 0.0), ([[2]], 2.0), ([3, 4], math.nan), ([True], math.nan),
    ({}, math.nan), (None, 0.0), (True, 1.0), (2.5, 2.5),
], ids=lambda v: repr(v)[:24])
def test_fake_js_to_number_matches_js_number(value, expected):
    """``_js_to_number`` is ECMAScript ``Number()`` for a wire value (releaseTask's ``Number(op.failure_limit)``)."""
    got = fake_board._js_to_number(value)
    assert (math.isnan(got) and math.isnan(expected)) or got == expected


@pytest.mark.parametrize("limit,ok", [("\u0663", False), ("\x1c3", False), ("\ufeff3", True), ("0x3", True),
                                      (["3"], True), ([], False), ([2, 3], False)], ids=lambda v: repr(v)[:16])
def test_fake_release_failure_limit_uses_js_number(limit, ok):
    """The release's ``failure_limit`` check reads the value as releaseTask's ``Number()`` does."""
    assert (FakeService.release_inputs({"failure_limit": limit}) == {}) is ok


@pytest.mark.parametrize("value,expected", [
    (None, "null"), (True, "true"), (False, "false"), (0, "0"), (1.5, "1.5"), (1e21, "1e+21"),
    (float("nan"), "NaN"), (float("inf"), "Infinity"), (-10**400, "-Infinity"), ("x", "x"), ([], ""),
    (["transient"], "transient"), ([["transient"]], "transient"), (["transient", "x"], "transient,x"),
    ([None, 1, [2, None]], ",1,2,"), ({}, "[object Object]"), ([{}], "[object Object]"),
], ids=lambda v: repr(v)[:24])
def test_fake_js_string_matches_js_string(value, expected):
    """``_js_string`` is ECMAScript ``String()`` for a wire value (node: ``String([null, 1, [2, null]])`` is
    ``",1,2,"``)."""
    assert fake_board._js_string(value) == expected


@pytest.mark.parametrize("block_kind,ok", [
    (["transient"], True), ([["assignment"]], True), (["capability"], True), (["transient", "x"], False),
    ([], False), ([None], False), ({}, False), (True, False), ("transient,", False),
], ids=lambda v: repr(v)[:24])
def test_fake_release_block_kind_coerced_like_js_string(block_kind, ok):
    """releaseTask checks ``String(op.block_kind ?? "transient")``, so the Board accepts ``["transient"]`` (an
    array String()s to its comma join) and refuses ``[]``, ``{}`` and ``true``."""
    assert (FakeService.release_inputs({"block_kind": block_kind}) == {}) is ok


@pytest.mark.parametrize("route,lease_runs", [("leases", True), ("leases", False), ("ops", True)])
@pytest.mark.parametrize("outcome", ["", 0, False, [], ["crashed", "x"], {}], ids=lambda v: repr(v)[:16])
def test_fake_release_outcome_without_nullish_default_is_invalid(task_seed, route, lease_runs, outcome):
    """``String(op.outcome ?? "released")``: only a missing or null outcome defaults, so ``""``, 0 and false stay
    themselves (``""``, ``"0"``, ``"false"``) and are refused ``invalid``, on both routes, changing nothing."""
    svc = FakeService(lease_runs=lease_runs)
    hub, tid = _leased(task_seed, svc)
    before = copy.deepcopy(svc.board(BOARD))
    res = _release(route, svc, hub, tid, outcome=outcome)
    assert (res["status"], res["reason"]) == ("rejected", "invalid")
    assert svc.board(BOARD) == before


@pytest.mark.parametrize("route", ["leases", "ops"])
@pytest.mark.parametrize("fields,run_outcome,error", [
    ({"outcome": None}, "released", None),  # null ?? "released"
    ({}, "released", None),
    ({"outcome": ["crashed"]}, "crashed", "crashed"),  # String(["crashed"]) is "crashed"
    ({"outcome": "crashed", "error": None}, "crashed", "crashed"),  # String(op.error ?? outcome)
    ({"outcome": "crashed", "error": ""}, "crashed", ""),  # "" is not nullish: the run's error is ""
    ({"outcome": "timed_out", "error": False}, "timed_out", "false"),
], ids=["null", "absent", "array", "null-error", "empty-error", "false-error"])
def test_fake_release_outcome_and_error_use_nullish_default(task_seed, route, fields, run_outcome, error):
    """Both routes read the outcome as ``String(op.outcome ?? "released")`` and the error as ``String(op.error ??
    outcome)``: a null outcome is a plain release, an explicit ``""`` error is kept."""
    svc = FakeService(lease_runs=True)
    hub, tid = _leased(task_seed, svc)
    assert _release(route, svc, hub, tid, **fields)["status"] == "applied"
    run = svc.board(BOARD)["rows"]["task_runs"][RUN]
    assert (run["outcome"], run["error"]) == (run_outcome, error)


@pytest.mark.parametrize("max_retries,failure_limit,status", [
    (0, None, "triage"), (None, "1", "triage"), (None, "0x2", "ready"), (None, None, "ready"), (1, 5, "triage"),
], ids=["max-retries-0", "limit-1", "limit-hex-2", "default-2", "max-retries-wins"])
def test_fake_release_op_failure_limit_nullish(task_seed, max_retries, failure_limit, status):
    """recordTaskFailure's limit is ``row.max_retries ?? (Number(op.failure_limit) ?? 2)``: a card max_retries of 0
    trips the first crash (not the ``or`` fallback to 2), and the op's limit is read with ``Number()``."""
    svc = FakeService(lease_runs=True)
    hub, tid = _leased(task_seed, svc)
    svc.board(BOARD)["rows"]["tasks"][tid]["max_retries"] = max_retries
    fields = {} if failure_limit is None else {"failure_limit": failure_limit}
    assert _release("ops", svc, hub, tid, outcome="crashed", **fields)["status"] == "applied"
    assert svc.board(BOARD)["rows"]["tasks"][tid]["status"] == status


def test_release_local_change_during_read_vetoes(live, svc, monkeypatch):
    lost_release(live, svc)
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read_then_open_run(board, table, uid):
        evidence = original(board, table, uid)
        if table == "task_runs":
            with kbc.write_txn(live):
                live.execute("INSERT INTO kanban_sync_tombstones (table_name,row_uid,deleted_at) VALUES ('task_runs',?,1)",
                             (RUN,))
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read_then_open_run)
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = resolve_rejections(live, hub, BOARD)[0]
    assert report["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1


def test_concurrent_edit_vetoes_retirement(live, svc, monkeypatch):
    reject_status(live, svc)
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read_then_change(*args):
        evidence = original(*args)
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_sync_outbox SET attempts=attempts+1 WHERE state='rejected'")
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read_then_change)
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    report = resolve_rejections(live, hub, BOARD)
    assert report[0]["classification"] == "unresolved"
    assert "changed" in report[0]["proof"]
    assert client.status(live)["rejected"] == 1


@pytest.mark.parametrize("veto", ["pending_create", "deleted", "history_compacted", "service_error"])
def test_moot_requires_proven_never_existed(live, svc, veto):
    ghost(live)
    if veto == "pending_create":
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('create-op','tasks','t_ghost','upsert','{}','',2)")
    elif veto == "deleted":
        svc.board(BOARD)["rows"].setdefault("tasks", {})["t_ghost"] = {"uid": "t_ghost", "deleted_at": 5}
    elif veto == "history_compacted":
        svc.board(BOARD)["floor"] = 1
    else:
        svc.offline = True
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    result = resolve_rejections(live, svc.hub(ME), BOARD)
    assert result[0]["classification"] == "unresolved"
    assert client.status(live)["rejected"] == 1
