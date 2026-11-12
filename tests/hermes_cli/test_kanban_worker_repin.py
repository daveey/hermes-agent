"""Explicit origin repin of an unstarted worker-first card (hermes_cli.kanban_worker_repin). [daveey]

Entry paths: ``kanban_move.move_queued`` (core API) and ``hermes kanban move`` (CLI), plus the real HTTP client
against a stub Board for CAS/refusal/outage. The local policy row is never edited: it changes only when the
authoritative feed is pulled.
"""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import NoReturn

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move as km
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_placement as wp
from hermes_cli import kanban_worker_repin as repin
from tests.hermes_cli import test_kanban_worker_node as node_fixtures
from tests.hermes_cli import test_kanban_worker_policy_db as db_fixtures
from tests.hermes_cli.kanban_sync_fake_board import FakeHub
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB

conn = db_fixtures.conn
gws = node_fixtures.gws
svc = node_fixtures.svc


@pytest.fixture(autouse=True)
def pool_registry(monkeypatch):
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {
        "worker-1": {"labels": ["pool:worker", "capability:hub-write"]}}})


@pytest.mark.parametrize("status", ["ready", "scheduled", "todo"])
@pytest.mark.parametrize("available", [False, True])
def test_cli_started_pool_requires_live_complete_label_match(conn, monkeypatch, status, available):
    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_worker_node as wn
    tid = _acknowledged(conn, status=status, started_at=1, requires=json.dumps(["hub:write"]))
    target = ["pool:worker", "capability:hub-write"]
    machines = {
        "plain": {"labels": ["pool:worker"]},
        "held": {"labels": target, "hold": True},
        "offline": {"labels": target, "state": "offline"},
    }
    if available:
        machines["capable"] = {"labels": target}
    monkeypatch.setattr(wn, "registry", lambda: {"machines": machines})
    before = list(conn.iterdump())
    calls = []

    class Hub:
        ident = NoCallHub.ident

        def repin(self, board, **kw):
            calls.append(kw)
            return {"version": 5}

    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: Hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda *a, **kw: Hub())
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: None)
    out = json.loads(kcli.run_slash(f'move {tid} --pool --reason "queue" --json'))
    if available:
        assert out["ok"] and out["matches"] == ["capable"], out
        assert calls == [{"task_id": tid, "expected_revision": 4, "runs_on": target}]
        assert _moved(conn, tid)[-1]["matches"] == ["capable"]
        text = kcli.run_slash(f'move {tid} --pool --reason "queue"')
        assert "eligible: capable" in text
    else:
        assert not out["ok"] and out["reason_code"] == "no_machine", out
        assert out["error"] == f"no active, unheld machine has every label in {target}"
        assert calls == []
        assert list(conn.iterdump()) == before  # row, reservation, outbox and every event unchanged


def test_pool_registry_unavailable_refuses_without_write(conn, monkeypatch):
    from hermes_cli import kanban_worker_node as wn
    tid = _acknowledged(conn, started_at=1)
    monkeypatch.setattr(wn, "registry", lambda: None)
    before = list(conn.iterdump())
    out = km.move_queued(conn, tid, runs_on=["pool:worker"], reason="queue", hub=NoCallHub())
    assert out["reason_code"] == "registry_unavailable"
    assert list(conn.iterdump()) == before


class RepinHub(FakeHub):
    """FakeHub plus the Board's ``repinWorker`` (worker/src/worker_placement.ts): creator only, ready and unleased,
    exact revision, then clear policy + set the pin in one Board write."""

    def __init__(self, svc, gateway, *, during=None):
        super().__init__(svc, gateway)
        self.repins, self.during = [], during

    def repin(self, board, *, task_id, expected_revision, runs_on):
        self._check()
        self.repins.append({"task_id": task_id, "expected_revision": expected_revision, "runs_on": runs_on})
        if self.during:
            self.during()
        b = self.svc.board(board)
        task = b["rows"].get("tasks", {}).get(task_id)
        def refuse(reason) -> NoReturn:
            raise ksc.HubError(409, json.dumps({"error": reason}))
        if task is None:
            refuse("not_found")
        if task.get("created_on") != self.gateway:
            refuse("not_creator")
        status = task.get("status")
        if status not in ("ready", "scheduled", "todo") or task_id in b["leases"]:
            refuse("not_claimable")
        # Waiting-state extension: server contract owned/tested in hermes-hub worker_placement.ts.
        if status != "ready":
            if task.get("placement_policy") != wp.POLICY:
                refuse("invalid_policy")
            if "pool:worker" not in runs_on or any(
                    label != "pool:worker" and not label.startswith(("capability:", "profile:", "skill:"))
                    for label in runs_on):
                refuse("invalid_pool_pin")
        if expected_revision != task["version"]:
            refuse("revision_conflict")
        row = self.svc._put(b, "tasks", task_id, {"placement_policy": None, "runs_on": json.dumps(runs_on)},
                            self.gateway, None, f"{9_999_999_999_997:013d}-0000-board")
        return {"version": row["version"], "runs_on": runs_on, "placement_policy": None}


def _local(conn_, tid):
    row = conn_.execute("SELECT placement_policy, runs_on, status FROM tasks WHERE id=?", (tid,)).fetchone()
    return row["placement_policy"], json.loads(row["runs_on"]) if row["runs_on"] else None, row["status"]


def _policy_card(origin):
    origin.enter()
    tid = kb.create_task(origin.conn, title="Public analysis", assignee="default", placement_policy=wp.POLICY)
    origin.round()
    origin.round()  # the second round reads the Board's echo, which carries the server-owned created_on
    row = origin.conn.execute("SELECT sync_version, status, created_on FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["sync_version"] is not None and row["status"] == "ready" and row["created_on"] == HUB
    return tid, row["sync_version"]


def _moved(conn_, tid):
    return [json.loads(p or "{}") for (p,) in conn_.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, km.MOVED_EVENT))]


# --- core API: Board repin, then authoritative readback ------------------------------------------------------------

def test_origin_repin_uses_board_cas_and_local_policy_changes_only_by_pull(gws, svc):
    origin, _ = gws
    tid, revision = _policy_card(origin)
    seen = []
    hub = RepinHub(svc, HUB, during=lambda: seen.append(_local(origin.conn, tid)))
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="run it on the hub", hub=hub)
    assert res["ok"] and res["repinned"] and res["readback"] == "confirmed", res
    assert res["matches"] == [HUB]  # the origin repin names the machine it lands on
    assert hub.repins == [{"task_id": tid, "expected_revision": revision, "runs_on": ["machine:hub"]}]
    assert seen == [(wp.POLICY, None, "ready")]  # nothing local was edited before the Board answered
    assert _local(origin.conn, tid) == (None, ["machine:hub"], "ready")
    assert wp.execution_refusal(origin.conn, origin.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) is None
    [ev] = _moved(origin.conn, tid)
    assert ev["repinned"] is True and ev["reason"] == "run it on the hub" and ev["board_version"] == res["board_version"]
    # No local runs_on/policy edit was queued for push.
    pending = origin.conn.execute("SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? "
                                  "AND state='pending'", (tid,)).fetchall()
    assert not any({"runs_on", "placement_policy"} & set(json.loads(f[0])) for f in pending)


def test_pull_failure_after_repin_reports_committed_with_readback_pending(gws, svc, monkeypatch):
    origin, _ = gws
    tid, _ = _policy_card(origin)
    def down(*a, **kw):
        raise OSError("feed unreachable")
    monkeypatch.setattr(ksc, "pull", down)
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="run it here", hub=RepinHub(svc, HUB))
    assert res["ok"] and res["readback"] == "pending" and "committed" in res["note"], res
    assert _local(origin.conn, tid) == (wp.POLICY, None, "ready")  # the fence holds until readback
    assert svc.board(BOARD)["rows"]["tasks"][tid]["placement_policy"] is None
    monkeypatch.undo()
    origin.round()  # the normal sync loop applies the Board's row
    assert _local(origin.conn, tid) == (None, ["machine:hub"], "ready")


@pytest.mark.parametrize("target", [["machine:worker-1"], ["machine:hub", "linux"], ["linux"]])
def test_non_origin_target_refuses_instead_of_a_stripped_local_relabel(gws, svc, target):
    origin, _ = gws
    tid, _ = _policy_card(origin)
    hub = RepinHub(svc, HUB)
    res = km.move_queued(origin.conn, tid, runs_on=target, reason="x", hub=hub)
    assert res["reason_code"] == "repin_unavailable" and not res["ok"], res
    assert hub.repins == [] and _local(origin.conn, tid) == (wp.POLICY, None, "ready") and not _moved(origin.conn, tid)


def test_board_offline_reports_unknown_outcome_with_local_copy_intact(gws, svc):
    origin, _ = gws
    tid, _ = _policy_card(origin)
    svc.offline = True
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x", hub=RepinHub(svc, HUB))
    assert res["reason_code"] == "outcome_unknown" and "local copy unchanged" in res["error"], res
    assert "Board outcome unknown" in res["error"]
    assert _local(origin.conn, tid) == (wp.POLICY, None, "ready") and not _moved(origin.conn, tid)


def test_board_revision_conflict_leaves_policy_intact(gws, svc):
    # The Board row changes on every attempt (a queued card's row is rewritten by each reservation reply): every
    # CAS loses, so after the bounded pull-and-retry the move is refused and the policy is untouched everywhere.
    origin, _ = gws
    tid, _ = _policy_card(origin)
    b = svc.board(BOARD)
    n = iter(range(9_999_999_999_990, 9_999_999_999_900, -1))
    churn = lambda: svc._put(b, "tasks", tid, {"priority": 3}, "worker-1", None, f"{next(n):013d}-0000-x")
    hub = RepinHub(svc, HUB, during=churn)
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x", hub=hub)
    assert res["reason_code"] == "revision_conflict", res
    assert len(hub.repins) == repin.CONFLICT_ATTEMPTS
    assert _local(origin.conn, tid)[0] == wp.POLICY and b["rows"]["tasks"][tid]["placement_policy"] == wp.POLICY


def test_repin_after_one_unseen_board_edit_pulls_and_retries(gws, svc):
    # Live: fixture t_01a1a46cbd08b275's printed move was refused revision_conflict because the Board rewrote the
    # row between the local read and the POST. One unseen edit: pull, re-check, CAS again at the new revision.
    origin, _ = gws
    tid, _ = _policy_card(origin)
    b = svc.board(BOARD)
    svc._put(b, "tasks", tid, {"priority": 3}, "worker-1", None, f"{9_999_999_999_996:013d}-0000-x")  # unseen edit
    hub = RepinHub(svc, HUB)
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x", hub=hub)
    assert res.get("ok") is True and res["readback"] == "confirmed", res
    assert len(hub.repins) == 2 and hub.repins[1]["expected_revision"] > hub.repins[0]["expected_revision"]
    assert _local(origin.conn, tid) == (None, ["machine:hub"], "ready")


def test_repin_retry_rechecks_preconditions_on_the_pulled_row(gws, svc, monkeypatch):
    # The conflicting edit blocked the card: the pulled row is no longer ready, so no second CAS is sent.
    origin, _ = gws
    tid, _ = _policy_card(origin)
    b = svc.board(BOARD)
    svc._put(b, "tasks", tid, {"priority": 3}, "worker-1", None, f"{9_999_999_999_996:013d}-0000-x")  # unseen edit
    real_pull = ksc.pull

    def pull_sees_block(conn_, *a, **kw):
        out = real_pull(conn_, *a, **kw)
        with conn_:
            conn_.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        return out

    monkeypatch.setattr(ksc, "pull", pull_sees_block)
    hub = RepinHub(svc, HUB)
    res = km.move_queued(origin.conn, tid, runs_on=["machine:hub"], reason="x", hub=hub)
    assert res.get("ok") is not True and res["reason_code"] == "repin_unavailable", res
    assert len(hub.repins) == 1 and b["rows"]["tasks"][tid]["placement_policy"] == wp.POLICY


# --- CLI entry path ------------------------------------------------------------------------------------------------

def test_cli_move_repins_an_unstarted_origin_policy_card(gws, svc, monkeypatch):
    from hermes_cli import kanban as kcli
    origin, _ = gws
    tid, _ = _policy_card(origin)
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", HUB))
    monkeypatch.setattr(ksc, "Hub", lambda ident, *a, **kw: RepinHub(svc, ident.gateway))
    out = json.loads(kcli.run_slash(f'move {tid} --runs-on machine:hub --reason "run on origin" --json'))
    assert out["ok"] and out["readback"] == "confirmed", out
    assert _local(origin.conn, tid) == (None, ["machine:hub"], "ready")


def test_cli_identity_for_another_gateway_is_refused(gws, svc, monkeypatch):
    from hermes_cli import kanban as kcli
    origin, _ = gws
    tid, _ = _policy_card(origin)
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", "worker-1"))
    monkeypatch.setattr(ksc, "Hub", lambda *a, **kw: pytest.fail("no Board call with a foreign identity"))
    out = json.loads(kcli.run_slash(f"move {tid} --runs-on machine:hub --reason x --json"))
    assert out["reason_code"] == "identity_mismatch" and _local(origin.conn, tid)[0] == wp.POLICY


# --- real HTTP client: refusals, CAS, no DB transaction across the call --------------------------------------------

def _acknowledged(conn_, **fields):
    tid = kb.create_task(conn_, title="Public analysis", assignee="default", placement_policy=wp.POLICY)
    conn_.execute("UPDATE kanban_sync_state SET applying=1")
    conn_.execute("UPDATE tasks SET sync_version=4, created_on='hub', status='ready' WHERE id=?", (tid,))
    for key, value in fields.items():
        conn_.execute(f"UPDATE tasks SET {key}=? WHERE id=?", (value, tid))
    conn_.execute("UPDATE kanban_sync_state SET applying=0")
    conn_.execute("DELETE FROM kanban_sync_outbox")
    conn_.commit()
    return tid


@pytest.fixture
def board_stub(conn):
    """A real HTTP server answering placement/repin with a configured status/body; records each request and
    whether the DB write lock was free while it was served."""
    state = {"status": 409, "body": {"error": "revision_conflict"}, "received": [], "lock_free": []}
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["received"].append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            if state.get("drop_after_commit"):
                state["committed"] = True
                self.close_connection = True
                return
            probe = sqlite3.connect(db_path, timeout=0)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
                state["lock_free"].append(True)
            except sqlite3.OperationalError:
                state["lock_free"].append(False)
            finally:
                probe.close()
            raw = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["hub"] = ksc.Hub(ksc.Identity(f"http://127.0.0.1:{server.server_port}", "synthetic", "hub"))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("status,body,code", [
    (409, {"error": "revision_conflict"}, "revision_conflict"),
    (409, {"error": "not_claimable"}, "repin_refused"),
    (409, {"error": "not_creator"}, "repin_refused"),
    (500, {"error": "secret internal detail"}, "outcome_unknown"),
    (502, {"error": "revision_conflict"}, "outcome_unknown"),
    (504, {"error": "timeout"}, "outcome_unknown"),
])
def test_http_refusal_leaves_policy_and_outbox_unchanged(conn, board_stub, status, body, code):
    tid = _acknowledged(conn)
    board_stub.update(status=status, body=body)
    res = km.move_queued(conn, tid, runs_on="machine:hub", reason="x", hub=board_stub["hub"])
    assert res["reason_code"] == code and not res["ok"], res
    assert "secret internal detail" not in res["error"]  # never echoes the raw Board body
    if status >= 500:
        assert "Board outcome unknown" in res["error"] and "refused" not in res["error"]
    assert board_stub["received"] == [("/v2/boards/default/placement/repin",
                                       {"task_id": tid, "expected_revision": 4, "runs_on": ["machine:hub"]})]
    assert board_stub["lock_free"] == [True]  # no write transaction held across the network call
    assert _local(conn, tid) == (wp.POLICY, None, "ready")
    # The admitted hub_admission audit event is the only write (the judge runs before the Board call).
    assert conn.execute("SELECT count(*) FROM kanban_sync_outbox WHERE table_name != 'task_events'"
                        ).fetchone()[0] == 0 and not _moved(conn, tid)
    assert [e.kind for e in kb.list_events(conn, tid) if e.kind == "hub_admission"] == ["hub_admission"]


def test_http_success_with_failed_readback_keeps_local_policy(conn, board_stub, monkeypatch):
    tid = _acknowledged(conn)
    board_stub.update(status=200, body={"version": 5, "runs_on": ["machine:hub"], "placement_policy": None})
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: (_ for _ in ()).throw(ksc.HubError(503, "down")))
    res = km.move_queued(conn, tid, runs_on="machine:hub", reason="x", hub=board_stub["hub"])
    assert res["ok"] and res["board_version"] == 5 and res["readback"] == "pending", res
    assert board_stub["lock_free"] == [True]
    assert _local(conn, tid) == (wp.POLICY, None, "ready")


@pytest.mark.parametrize("status", ["scheduled", "todo"])
@pytest.mark.parametrize("started", [False, True])
def test_waiting_http_success_with_failed_readback_retains_status(conn, board_stub, monkeypatch, status, started):
    tid = _acknowledged(conn, status=status, started_at=1 if started else None)
    # Exact 200 response contract from Board worker_placement.ts, verified by its HTTP tests.
    board_stub.update(status=200, body={"version": 5, "runs_on": ["pool:worker"], "placement_policy": None})
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: (_ for _ in ()).throw(ksc.HubError(503, "down")))
    res = km.move_queued(conn, tid, runs_on=["pool:worker"], reason="pool migration", hub=board_stub["hub"])
    assert res["ok"] and res["readback"] == "pending", res
    assert res["status"] == status
    assert board_stub["received"] == [("/v2/boards/default/placement/repin",
                                       {"task_id": tid, "expected_revision": 4, "runs_on": ["pool:worker"]})]
    assert _local(conn, tid) == (wp.POLICY, None, status)
    assert _moved(conn, tid)[-1]["repinned"] is True


def test_http_transport_failure_has_unknown_outcome(conn):
    tid = _acknowledged(conn)
    hub = ksc.Hub(ksc.Identity("http://127.0.0.1:9", "synthetic", "hub"), timeout=2)
    res = km.move_queued(conn, tid, runs_on="machine:hub", reason="x", hub=hub)
    assert res["reason_code"] == "outcome_unknown" and _local(conn, tid) == (wp.POLICY, None, "ready")


def test_committed_request_with_lost_reply_never_claims_policy_unchanged(conn, board_stub):
    tid = _acknowledged(conn)
    board_stub["drop_after_commit"] = True
    res = km.move_queued(conn, tid, runs_on="machine:hub", reason="x", hub=board_stub["hub"])
    assert board_stub["committed"] and len(board_stub["received"]) == 1
    assert not res["ok"] and res["reason_code"] == "outcome_unknown"
    assert "Board outcome unknown" in res["error"] and "policy unchanged" not in res["error"]
    assert _local(conn, tid) == (wp.POLICY, None, "ready")


class NoCallHub:
    ident = ksc.Identity("https://hub.test", "k", "hub")

    def repin(self, *a, **kw):
        pytest.fail("refused locally: no Board call")


@pytest.mark.parametrize("fields,code", [
    ({"created_on": "other"}, "repin_unavailable"),
    ({"status": "todo"}, "repin_unavailable"),
    ({"status": "blocked"}, "repin_unavailable"),
    ({"claim_lock": "local:1"}, "repin_unavailable"),
    ({"lease_holder": "worker-1", "lease_expires": 1}, "repin_unavailable"),
    ({"started_at": 1}, "repin_unavailable"),
    ({"current_run_id": 7}, "repin_unavailable"),
    ({"sync_version": None}, "not_acknowledged"),
    ({"placement_policy": "other-policy"}, "repin_unavailable"),
])
def test_ineligible_policy_cards_refuse_without_network_or_mutation(conn, fields, code):
    tid = _acknowledged(conn, **fields)
    before = conn.execute("SELECT placement_policy, runs_on FROM tasks WHERE id=?", (tid,)).fetchone()
    res = km.move_queued(conn, tid, runs_on=["machine:hub"], reason="x", hub=NoCallHub())
    assert res["reason_code"] == code and not res["ok"], res
    assert tuple(conn.execute("SELECT placement_policy, runs_on FROM tasks WHERE id=?", (tid,)).fetchone()) == tuple(before)
    assert not _moved(conn, tid)


def test_prior_run_row_refuses_as_started(conn):
    tid = _acknowledged(conn)
    conn.execute("INSERT INTO task_runs(task_id, status, started_at) VALUES(?, 'released', 1)", (tid,))
    conn.commit()
    res = km.move_queued(conn, tid, runs_on=["machine:hub"], reason="x", hub=NoCallHub())
    assert res["reason_code"] == "repin_unavailable" and "started" in res["error"]


def test_pending_task_outbox_refuses_as_unacknowledged(conn):
    tid = _acknowledged(conn)
    conn.execute("UPDATE tasks SET priority=2 WHERE id=?", (tid,))
    conn.commit()
    res = km.move_queued(conn, tid, runs_on=["machine:hub"], reason="x", hub=NoCallHub())
    assert res["reason_code"] == "not_acknowledged"


def test_hub_identity_must_match_board_gateway(conn):
    tid = _acknowledged(conn)
    hub = NoCallHub()
    hub.ident = ksc.Identity("https://hub.test", "k", "worker-1")
    res = km.move_queued(conn, tid, runs_on=["machine:hub"], reason="x", hub=hub)
    assert res["reason_code"] == "identity_mismatch"


def test_manual_and_running_cards_keep_the_generic_path(conn):
    manual = kb.create_task(conn, title="manual", assignee="default")
    assert not repin.applies(conn, manual)
    res = km.move_queued(conn, manual, runs_on=["machine:hub"], reason="x", machines={}, hub=NoCallHub())
    assert res["reason_code"] == "no_machine"  # unchanged: the generic move never targets this machine
    for status in ("running", "done", "archived"):
        assert not repin.applies(conn, _acknowledged(conn, status=status))


# --- --pool: any worker-first card that is ready goes to the one worker queue (t_01a1d0571c2f07d6) -----------------
# 11-12 13:15Z: t_01a1cf7624ae27d9 (worker-first, started once, 765 re-reservations onto one worker) could not be
# moved: `move --pool` refused repin_unavailable, and the origin-repin workaround only covers unstarted cards.

def _started(origin, tid):
    origin.conn.execute("INSERT INTO task_runs(task_id, status, started_at) VALUES(?, 'released', 1)", (tid,))
    origin.conn.commit()


def test_cli_move_pool_repins_a_started_worker_first_card(gws, svc, monkeypatch):
    from hermes_cli import kanban as kcli
    origin, _ = gws
    tid, revision = _policy_card(origin)
    _started(origin, tid)
    hubs = []
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", HUB))
    monkeypatch.setattr(ksc, "Hub", lambda ident, *a, **kw: hubs.append(RepinHub(svc, ident.gateway)) or hubs[-1])
    out = json.loads(kcli.run_slash(f'move {tid} --pool --reason "one worker reserved for 4 h" --json'))
    assert out["ok"] and out["readback"] == "confirmed" and out["runs_on"] == ["pool:worker"], out
    assert hubs[0].repins == [{"task_id": tid, "expected_revision": revision, "runs_on": ["pool:worker"]}]
    assert _local(origin.conn, tid) == (None, ["pool:worker"], "ready")
    [ev] = _moved(origin.conn, tid)
    assert ev["repinned"] is True and ev["runs_on"] == ["pool:worker"] and ev["previous_runs_on"] is None, ev
    assert out["matches"] == ["worker-1"] == ev["matches"]


@pytest.mark.parametrize("status", ["scheduled", "todo"])
@pytest.mark.parametrize("started", [False, True])
def test_cli_waiting_pool_repin_keeps_authoritative_status(gws, svc, monkeypatch, status, started):
    from hermes_cli import kanban as kcli
    origin, _ = gws
    tid, _ = _policy_card(origin)
    if started:
        _started(origin, tid)
    b = svc.board(BOARD)
    svc._put(b, "tasks", tid, {"status": status}, HUB, None, "9999999999996-0000-board")
    origin.round()
    assert _local(origin.conn, tid)[2] == status
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", HUB))
    monkeypatch.setattr(ksc, "Hub", lambda ident, *a, **kw: RepinHub(svc, ident.gateway))
    before = dict(b["rows"]["tasks"][tid])
    out = json.loads(kcli.run_slash(f'move {tid} --pool --reason "preserve waiting state" --json'))
    assert out["ok"] and out["readback"] == "confirmed" and out["status"] == status, out
    assert _local(origin.conn, tid) == (None, ["pool:worker"], status)
    after = b["rows"]["tasks"][tid]
    assert after["status"] == before["status"]
    assert _moved(origin.conn, tid)[-1]["repinned"] is True


def test_move_pool_keeps_the_cards_coordination_labels(gws, svc):
    origin, _ = gws
    origin.enter()
    tid = kb.create_task(origin.conn, title="Read ~/.hermes/logs on the hub", assignee="default",
                         placement_policy=wp.POLICY, requires=["hub:write"])
    origin.round()
    origin.round()
    hub = RepinHub(svc, HUB)
    res = km.move_queued(origin.conn, tid, runs_on=["pool:worker"], reason="any hub-write worker", hub=hub)
    assert res["ok"] and res["readback"] == "confirmed", res
    target = ["pool:worker", "capability:hub-write"]
    assert hub.repins[0]["runs_on"] == target  # only a hub-write worker claims it
    assert res["runs_on"] == target == _moved(origin.conn, tid)[-1]["runs_on"]
    assert _local(origin.conn, tid)[:2] == (None, target)


@pytest.mark.parametrize("fields,code", [
    ({"status": "blocked"}, "repin_unavailable"),
    ({"claim_lock": "local:1"}, "repin_unavailable"),
    ({"lease_holder": "worker-1", "lease_expires": 1}, "repin_unavailable"),
    ({"created_on": "other"}, "repin_unavailable"),
    ({"sync_version": None}, "not_acknowledged"),
])
def test_move_pool_still_refuses_what_the_board_refuses(conn, fields, code):
    tid = _acknowledged(conn, **fields)
    res = km.move_queued(conn, tid, runs_on=["pool:worker"], reason="x", hub=NoCallHub())
    assert res["reason_code"] == code and not res["ok"], res
    assert not _moved(conn, tid)


class _RecordHub(NoCallHub):
    def __init__(self):
        self.calls = []

    def repin(self, board, **kw):
        self.calls.append(kw)
        raise ksc.HubError(409, json.dumps({"error": "not_claimable"}))


@pytest.mark.parametrize("started", [{"started_at": 1}, {"current_run_id": 7}])
def test_move_pool_of_a_started_card_reaches_the_board(conn, started):
    tid = _acknowledged(conn, **started)
    hub = _RecordHub()
    res = km.move_queued(conn, tid, runs_on=["pool:worker"], reason="x", hub=hub)
    assert [c["runs_on"] for c in hub.calls] == [["pool:worker"]], res
    origin = km.move_queued(conn, tid, runs_on=["machine:hub"], reason="x", hub=NoCallHub())
    assert origin["reason_code"] == "repin_unavailable" and "started" in origin["error"]  # origin repin unchanged


@pytest.mark.parametrize("status", ["scheduled", "todo"])
@pytest.mark.parametrize("started", [False, True])
def test_waiting_pool_repin_eligibility_preserves_state(conn, status, started):
    tid = _acknowledged(conn, status=status, started_at=1 if started else None)
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    result = repin.eligibility(conn, tid, ["pool:worker"])
    assert result["ok"], result
    assert result["status"] == status
    assert result["labels"] == ["pool:worker"]
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    origin = repin.eligibility(conn, tid, ["machine:hub"])
    assert not origin["ok"] and "not ready" in origin["error"]


@pytest.mark.parametrize("status", ["scheduled", "todo"])
@pytest.mark.parametrize("fields,fragment", [
    ({"claim_lock": "local:1"}, "holds a claim"),
    ({"lease_holder": "worker-1"}, "holds a claim"),
    ({"created_on": "other"}, "was created on"),
    ({"sync_version": None}, "not acknowledged"),
])
def test_waiting_pool_repin_keeps_ownership_fences(conn, status, fields, fragment):
    tid = _acknowledged(conn, status=status, **fields)
    result = km.move_queued(conn, tid, runs_on=["pool:worker"], reason="x", hub=NoCallHub())
    assert not result["ok"] and fragment in result["error"], result
    assert not _moved(conn, tid)


@pytest.mark.parametrize("status", ["blocked", "review", "running", "done", "archived"])
def test_pool_repin_rejects_nonwaiting_status(conn, status):
    tid = _acknowledged(conn, status=status)
    result = repin.eligibility(conn, tid, ["pool:worker"])
    assert not result["ok"] and f"it is {status}, not" in result["error"], result


def test_move_to_another_single_machine_still_refuses(conn):
    tid = _acknowledged(conn)
    res = km.move_queued(conn, tid, runs_on=["machine:worker-1"], reason="x", hub=NoCallHub())
    assert res["reason_code"] == "repin_unavailable"


def test_move_pool_refuses_a_card_whose_action_no_worker_is_labelled_for(conn):
    tid = _acknowledged(conn, title="Run install-boards.py on the hub")
    res = km.move_queued(conn, tid, runs_on=["pool:worker"], reason="x", hub=NoCallHub())
    assert res["reason_code"] == "repin_unavailable" and "no pool worker" in res["error"], res
