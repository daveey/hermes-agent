"""Partitioned endpoint proof never imports a task or authorizes an old unlink."""
import contextlib
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_dependency_delete as r
from tests.hermes_cli import test_kanban_sync_dependency_delete as base

BOARD, ME, rows = base.BOARD, base.ME, base.rows
case = base.case

FOREIGN = "worker-1"
REASON = "retain foreign dependency"


@pytest.fixture
def foreign(case, monkeypatch):
    live, hub, svc, _uid, parent, _child = case
    canonical = svc.board(BOARD)["rows"]["tasks"][parent]
    canonical["created_on"] = FOREIGN
    svc._put(svc.board(BOARD), "tasks", parent, {"status": "done"}, FOREIGN, None,
             "9999999999999-0000-worker-1")
    live.execute("PRAGMA foreign_keys=OFF")
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying=1")
        live.execute("DELETE FROM tasks WHERE id=?", (parent,))
        live.execute("UPDATE kanban_sync_state SET applying=0")
    original = hub.containment_context
    monkeypatch.setattr(hub, "containment_context", lambda b, t, p=None: {
        **original(b, t, p), "target": {"board": b, "gateway": FOREIGN if t == parent else ME, "task_id": t}})
    return case


def protected(live):
    return {t: rows(live, t) for t in ("tasks", "task_links", "task_events", "task_runs")}


def test_certified_release_then_renewal_in_full_replica(foreign, monkeypatch):
    # Authenticated transport double + real certificate cache/guard/apply path.
    # This is fixture-only evidence, not authority to resume any live task.
    from hermes_cli import kanban_containment_replica as cert
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_containment_connections import held
    from hermes_cli.kanban_containment_state import fingerprint
    from tests.hermes_cli import test_kanban_containment_canonical as wire
    live, hub, svc, uid, parent, _ = foreign
    assert r.plan(live, hub, BOARD, uid, REASON)["eligible"]
    shadow = client.open_shadow(BOARD, ME)
    head = sync.sync_state(shadow)["cursor"]
    stop, payload, episode = wire.operation()
    payload["target"].update(board=BOARD, gateway=FOREIGN, task_id=parent)
    payload["policy"]["authority"].update(board=BOARD)
    stop.update(task=parent, payload=payload)
    release, _, _ = wire.operation("resume_contained")
    release.update(task=parent, payload={"version": 1, "episode": episode,
        "containment_sha256": fingerprint(payload), "resume_status": "ready",
        "evidence": {"kind": "authority", "authority": payload["policy"]["authority"], "run_uid": sync.uuid7()}})
    proofs = {}
    for op in (stop, release):
        e = wire.evidence(op, payload, episode)
        e["board"] = BOARD
        e["row"].update(task_id=parent, created_at=1)
        proofs[op["event_uid"]] = e
    original = hub.row_evidence
    monkeypatch.setattr(hub, "row_evidence", lambda b, t, u: proofs[u] if t == "task_events" else original(b, t, u))
    def project(op):
        change = client._row_change("task_events", proofs[op["event_uid"]]["row"])
        cert.certify_page(shadow, hub, BOARD, [change])
        sync.apply_remote_changes(shadow, [change], new_cursor=head, board=BOARD, observers=False)
    before = protected(live)
    try:
        project(stop)
        assert held(shadow, parent)
        assert r.plan(live, hub, BOARD, uid, REASON).get("reason") == "replica_containment_hold"
        project(release)
        assert not held(shadow, parent)
        # Mirror the natural release's canonical task row in the feed as well.
        svc._put(svc.board(BOARD), "tasks", parent, {"status": "ready"}, FOREIGN, None,
                 "9999999999999-0001-worker-1")
        p = r.plan(live, hub, BOARD, uid, REASON)
        assert p["eligible"], p
        assert r.apply(live, hub, BOARD, uid, p["token"], REASON)["action"] == "applied"
        # The release's task feed advanced the replica; retain its fresh cut.
        head = sync.sync_state(shadow)["cursor"]
        # A successor episode is not covered by that prior release or receipt.
        renewed = {**stop, "event_uid": sync.uuid7(), "client_op_id": sync.uuid7()}
        e = wire.evidence(renewed, payload, renewed["event_uid"])
        e["board"] = BOARD
        e["row"].update(task_id=parent, created_at=2)
        proofs[renewed["event_uid"]] = e
        project(renewed)
        op = rows(live, "kanban_sync_outbox")[0]
        result = r.settle(live, hub, BOARD, op, r.receipt_for_op(live, op))
        assert result["action"] == "retained" and result["proof"] == "replica_containment_hold", result
        assert len(rows(live, "kanban_sync_outbox")) == 3
        assert protected(live) == before
    finally:
        shadow.close()


def test_foreign_preview_apply_settle_never_materializes_or_writes_lifecycle(foreign):
    live, hub, _, uid, parent, _ = foreign
    before = protected(live)
    writes = []
    live.set_trace_callback(writes.append)
    p = r.plan(live, hub, BOARD, uid, REASON)
    assert p["eligible"], p
    assert protected(live) == before
    assert r.apply(live, hub, BOARD, uid, p["token"], REASON)["action"] == "applied"
    receipt = rows(live, r.TABLE)[0]
    assert json.loads(receipt["evidence_json"])["foreign"]["tasks"][0]["id"] == parent
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    reports = resolve_rejections(live, hub, BOARD)
    assert len(reports) == 3 and all(x["action"] == "retired" for x in reports), reports
    assert protected(live) == before
    assert not live.execute("SELECT 1 FROM tasks WHERE id=?", (parent,)).fetchone()
    lifecycle = ("TASKS", "TASK_LINKS", "TASK_EVENTS", "TASK_RUNS")
    assert not [s for s in writes if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                and any(f" {t} " in s.upper() for t in lifecycle)]


@pytest.mark.parametrize("where", ["local", "canonical", "replica"])
def test_foreign_valid_held_reaches_containment_refusal(foreign, monkeypatch, where):
    live, hub, _, uid, parent, child = foreign
    if where == "local":
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_sync_state SET applying=1")
            live.execute("UPDATE tasks SET containment_episode='renewed' WHERE id=?", (child,))
            live.execute("UPDATE kanban_sync_state SET applying=0")
    elif where == "canonical":
        original = hub.containment_context
        monkeypatch.setattr(hub, "containment_context", lambda *a: {**original(*a), "held": a[1] == parent})
    else:
        assert not client._pull_replica(BOARD, hub, ME, diagnostic=True).busy
        shadow = client.open_shadow(BOARD, ME)
        with kbc.write_txn(shadow):
            shadow.execute("UPDATE kanban_sync_state SET applying=1")
            shadow.execute("UPDATE tasks SET containment_episode='renewed' WHERE id=?", (parent,))
            shadow.execute("UPDATE kanban_sync_state SET applying=0")
        shadow.close()
        monkeypatch.setattr(client, "_pull_replica", lambda *a, **kw: client.Pulled())
    before = protected(live)
    p = r.plan(live, hub, BOARD, uid, REASON)
    assert p.get("reason") in ("local_containment_hold", "canonical_containment_hold", "replica_containment_hold"), p
    assert protected(live) == before
    assert len(rows(live, "kanban_sync_outbox")) == 3
    assert rows(live, "kanban_sync_tombstones")


@pytest.mark.parametrize("defect", ["local_origin", "missing_origin", "wrong_origin", "wrong_version", "missing_replica",
                                  "context_origin", "zero_version", "bool_version", "replica_origin", "replica_lease"])
def test_foreign_provenance_required(foreign, monkeypatch, defect):
    live, hub, _, uid, parent, _ = foreign
    assert not client._pull_replica(BOARD, hub, ME, diagnostic=True).busy
    monkeypatch.setattr(client, "_pull_replica", lambda *a, **kw: client.Pulled())
    shadow = client.open_shadow(BOARD, ME)
    if defect == "missing_replica":
        shadow.execute("PRAGMA foreign_keys=OFF")
        with kbc.write_txn(shadow):
            shadow.execute("UPDATE kanban_sync_state SET applying=1")
            shadow.execute("DELETE FROM tasks WHERE id=?", (parent,))
            shadow.execute("UPDATE kanban_sync_state SET applying=0")
    elif defect in ("replica_origin", "replica_lease"):
        with kbc.write_txn(shadow):
            shadow.execute("UPDATE kanban_sync_state SET applying=1")
            if defect == "replica_origin": shadow.execute("UPDATE tasks SET created_on='wrong' WHERE id=?", (parent,))
            else: shadow.execute("UPDATE tasks SET lease_holder='new-holder' WHERE id=?", (parent,))
            shadow.execute("UPDATE kanban_sync_state SET applying=0")
    shadow.close()
    original = hub.row_evidence
    def evidence(b, table, task):
        e = original(b, table, task)
        if table == "tasks" and task == parent:
            if defect == "local_origin": e["row"]["created_on"] = ME
            if defect == "missing_origin": e["row"]["created_on"] = None
            if defect == "wrong_origin": e["row"]["created_on"] = "other"
            if defect == "wrong_version": e["row"]["version"] += 1
            if defect == "zero_version": e["row"]["version"] = 0
            if defect == "bool_version": e["row"]["version"] = True
        return e
    monkeypatch.setattr(hub, "row_evidence", evidence)
    if defect == "context_origin":
        old = hub.containment_context
        monkeypatch.setattr(hub, "containment_context", lambda *a: {
            **old(*a), "target": {"board": BOARD, "gateway": "wrong", "task_id": a[1]}})
    before = protected(live)
    p = r.plan(live, hub, BOARD, uid, REASON)
    assert not p["eligible"], p
    assert protected(live) == before


@pytest.mark.parametrize("phase", ["preview", "apply", "settle"])
@pytest.mark.parametrize("change", ["cursor", "version", "held", "link"])
def test_foreign_replica_cas_at_evidence_or_writer_boundary(foreign, monkeypatch, phase, change):
    live, hub, _, uid, parent, _ = foreign
    p = r.plan(live, hub, BOARD, uid, REASON)
    assert p["eligible"], p
    if phase == "settle":
        assert r.apply(live, hub, BOARD, uid, p["token"], REASON)["action"] == "applied"
    def move():
        shadow = client.open_shadow(BOARD, ME)
        with kbc.write_txn(shadow):
            shadow.execute("UPDATE kanban_sync_state SET applying=1")
            if change == "cursor": shadow.execute("UPDATE kanban_sync_state SET cursor=cursor+1")
            if change == "version": shadow.execute("UPDATE tasks SET sync_version=sync_version+1 WHERE id=?", (parent,))
            if change == "held": shadow.execute("UPDATE tasks SET containment_episode='renewed' WHERE id=?", (parent,))
            if change == "link": shadow.execute("DELETE FROM task_links WHERE parent_id=?", (parent,))
            shadow.execute("UPDATE kanban_sync_state SET applying=0")
        shadow.close()
    if phase == "preview":
        old = hub.containment_context
        def context(*a):
            result = old(*a)
            if a[1] == parent: move()
            return result
        monkeypatch.setattr(hub, "containment_context", context)
    else:
        transaction = r.write_txn
        @contextlib.contextmanager
        def interleaved(conn):
            move()
            with transaction(conn):
                yield
        monkeypatch.setattr(r, "write_txn", interleaved)
    before = protected(live)
    if phase == "preview":
        result = r.plan(live, hub, BOARD, uid, REASON)
    elif phase == "apply":
        result = r.apply(live, hub, BOARD, uid, p["token"], REASON)
    else:
        op = rows(live, "kanban_sync_outbox")[0]
        result = r.settle(live, hub, BOARD, op, r.receipt_for_op(live, op))
    assert result["action"] in ("refused", "retained"), result
    assert result.get("reason", result.get("proof")) == "replica_inputs_changed", result
    assert protected(live) == before
    assert len(rows(live, "kanban_sync_outbox")) == 3


@pytest.mark.parametrize("change", ["work", "materialize", "lease", "renewed_hold"])
def test_foreign_endpoint_work_and_lease_races_refuse_old_preview(foreign, monkeypatch, change):
    live, hub, svc, uid, parent, _ = foreign
    p = r.plan(live, hub, BOARD, uid, REASON)
    assert p["eligible"], p
    if change == "work":
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('new','tasks',?,'upsert','{}','',9999)", (parent,))
    elif change == "materialize":
        original = hub.containment_context
        def context(*a):
            c = original(*a)
            if a[1] == parent:
                with kbc.write_txn(live):
                    live.execute("UPDATE kanban_sync_state SET applying=1")
                    live.execute("INSERT OR IGNORE INTO tasks(id,title,status,created_by,created_at,created_on) "
                                 "VALUES (?,'foreign','done','fixture',0,?)", (parent, FOREIGN))
                    live.execute("UPDATE kanban_sync_state SET applying=0")
            return c
        monkeypatch.setattr(hub, "containment_context", context)
    elif change == "lease":
        svc._put(svc.board(BOARD), "tasks", parent, {"lease_holder": "worker-2", "lease_expires": 9999999999999},
                 FOREIGN, None, "9999999999999-0001-worker-1")
    else:
        original = hub.containment_context
        monkeypatch.setattr(hub, "containment_context", lambda *a: {**original(*a), "held": True})
    result = r.apply(live, hub, BOARD, uid, p["token"], REASON)
    assert result["action"] == "refused", result
    assert rows(live, "kanban_sync_tombstones")
    assert len(rows(live, "kanban_sync_outbox")) >= 3
