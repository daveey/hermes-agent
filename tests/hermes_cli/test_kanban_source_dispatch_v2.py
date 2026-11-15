"""Protocol-2 grant transport and separate scratch/source workspace custody."""
from dataclasses import asdict
from types import SimpleNamespace
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_dispatch as dispatch
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_source_registration as registration
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import test_kanban_source_registration as fixtures

setup = fixtures.setup


def grant_snapshot(conn, tid, previous, *, fault=None):
    claimed = kb.get_task(conn, tid)
    uid = sync.run_uid(conn, claimed.current_run_id)
    reply = {"status": "applied", "lease": {"task_id": tid, "run_uid": uid,
             "holder": previous.machine, "expires": int(time.time() * 1000) + 900000},
             "source_predecessor": {"task_id": tid, "run_uid": previous.run_uid, "machine": previous.machine},
             "row": {"workspace_source": None, "workspace_custody": {
                 "source_holder": previous.machine, "source_protocol": 2, "manifest_revision": 0,
                 "state": "retained", "retained_run_uid": previous.run_uid}}}
    faults = {
        "predecessor": lambda: reply["source_predecessor"].__setitem__("run_uid", "unknown"),
        "lease_task": lambda: reply["lease"].__setitem__("task_id", "other"),
        "lease_expired": lambda: reply["lease"].__setitem__("expires", 1),
        "lease_holder": lambda: reply["lease"].__setitem__("holder", "other"),
    }
    if fault is not None:
        faults[fault]()
    hub = SimpleNamespace(leases=lambda *_: reply)
    lease = client.HubLeaseClient(hub).acquire(board_id="b", task_uid=tid, run_uid=uid,
                                             gateway_id=previous.machine, ttl_s=900, source_protocol=2)
    return lease.source_snapshot, reply


def successor(conn, tid):
    old = kb.get_task(conn, tid).current_run_id
    conn.execute("UPDATE task_runs SET status='released', ended_at=? WHERE id=?", (int(time.time()), old))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    conn.commit()
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")


def test_exact_grant_evidence_survives_transport_without_replica_authority(setup):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    previous = registration.register_initial(conn, tid, str(source))
    successor(conn, tid)
    snapshot, reply = grant_snapshot(conn, tid, previous)
    assert snapshot.grant == {k: reply[k] for k in ("status", "lease", "source_predecessor")}
    reply["source_predecessor"]["run_uid"] = "changed-after-read"
    assert snapshot.grant["source_predecessor"]["run_uid"] == previous.run_uid
    replica = ws.snapshot_from_row(reply["row"])
    assert replica.grant is None


@pytest.mark.parametrize("fault", [None, "predecessor", "lease_task", "lease_expired", "lease_holder"])
def test_real_resolve_and_attach_keep_scratch_cwd_and_external_source(setup, fault):
    conn, b, tid, source, scratch, _home, _clone = setup
    previous = registration.register_initial(conn, tid, str(source))
    successor(conn, tid)
    claimed = kb.get_task(conn, tid)
    snapshot, _reply = grant_snapshot(conn, tid, previous, fault=fault)
    ws.stash_grant_snapshot(tid, snapshot)
    workspace_api = SimpleNamespace(resolve_workspace=lambda *a, **k: scratch)
    before = len(b.calls)
    try:
        if fault:
            with pytest.raises(ws.WorkspaceSourceRefusal, match="source_identity_refused"):
                dispatch.resolve(conn, claimed, board=None, kbw=workspace_api)
            assert len(b.calls) == before
            assert board.attached_run(conn, claimed.current_run_id) is None
            return
        workspace, branch, resolved = dispatch.resolve(conn, claimed, board=None, kbw=workspace_api)
        assert workspace == scratch and resolved.path == str(source)
        assert branch == previous.branch and resolved.mode == "resume"
        run = launch.attach(conn, claimed, str(workspace), resolved, snapshot)
        assert run.path == str(source) and run.pre_head == previous.pre_head
        assert run.source_protocol == 2 and run.machine == previous.machine
        assert b.body("source_attach")["source_protocol"] == 2
        assert kb.get_task(conn, tid).workspace_path == str(scratch)
        assert asdict(snapshot.custody)["retained_run_uid"] == previous.run_uid
    finally:
        launch.drop_grant(tid)


@pytest.mark.parametrize("fault", ["missing_envelope", "bad_lease", "lost_lease_after_resolve", "offline", "refused"])
def test_successor_rechecks_before_attach_and_keeps_uncertain_journal(setup, fault):
    from dataclasses import replace
    conn, b, tid, source, scratch, _home, _clone = setup
    previous = registration.register_initial(conn, tid, str(source))
    successor(conn, tid)
    claimed = kb.get_task(conn, tid)
    snapshot, _reply = grant_snapshot(conn, tid, previous)
    if fault in {"missing_envelope", "bad_lease"}:
        grant = None if fault == "missing_envelope" else {**snapshot.grant, "lease": ["bad"]}
        snapshot = replace(snapshot, grant=grant)
        from hermes_cli.kanban_source_resolver import resolve_source_workspace
        with pytest.raises(ws.WorkspaceSourceRefusal, match="source_identity_refused"):
            resolve_source_workspace(conn, claimed, snapshot, me=previous.machine)
        return
    from hermes_cli.kanban_source_resolver import resolve_source_workspace
    resolved = resolve_source_workspace(conn, claimed, snapshot, me=previous.machine)
    if fault == "lost_lease_after_resolve":
        conn.execute("UPDATE tasks SET lease_expires=1 WHERE id=?", (tid,))
        conn.commit()
    elif fault == "offline":
        b.offline.add("source_attach")
    else:
        b.answer = lambda body: {"status": "rejected", "reason": "lease_lost"}
    with pytest.raises(launch.AttachRefused) as error:
        launch.attach(conn, claimed, str(scratch), resolved, snapshot)
    # M3: lease_lost precedes the Board's same-run attachment lookup, so it never proves nothing attached
    assert error.value.uncertain == (fault in {"offline", "refused"})
    observed = board.attached_run(conn, claimed.current_run_id)
    if fault == "lost_lease_after_resolve":
        assert observed is None
    else:
        assert observed.state == "pending" and observed.path == str(source)


def test_published_scratch_attach_observes_source_not_worker_cwd(setup):
    from hermes_cli.kanban_source_resolver import ResolvedSource
    from tests.hermes_cli.test_kanban_workspace_source import manifest
    conn, b, tid, source, scratch, _home, _clone = setup
    claimed = kb.get_task(conn, tid)
    raw = {**manifest(), "version": 2, "source_kind": "scratch_external",
           "repository": {"id": 1, "full_name": "example/source", "url": "https://github.com/example/source"}}
    snapshot = ws.SourceSnapshot(raw, ws.Custody("other", "published", 1, source_protocol=2))
    resolved = ResolvedSource(str(source), f"wt/{tid}", raw["commit"], 1, "materialized",
                              source_kind="scratch_external")
    run = launch.attach(conn, claimed, str(scratch), resolved, snapshot)
    assert run.path == str(source) and run.pre_head == fixtures.git(source, "rev-parse", "HEAD")
    assert run.source_protocol == 2 and run.machine == sync._state(conn)["gateway_id"]
    assert b.body("source_attach")["source_protocol"] == 2
