"""workspace_source v1 core PR-1 (t_01a1d0ec4ab93f32): manifest schema, grant snapshot threading, R4b routing,
R4c fence + compat, pull-only columns, config, S9 protocol refusal, pickup skip and the claim-side skip memo."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_workspace_source as kws
from tests.hermes_cli import kanban_sync_support as support

C1 = "a" * 40


def manifest(**over) -> dict:
    m = {"version": 1, "repository": {"id": 42, "full_name": "acme/widget", "url": "https://github.com/acme/widget"},
         "branch": "refs/heads/wt/t_1", "commit": C1, "base_commit": None, "published_at": 1,
         "manifest_revision": 1, "published_by": {"run_uid": "r1", "machine": "hub"}, "source_kind": "task_worktree",
         "artifacts": []}
    m.update(over)
    return m


def custody(**over) -> dict:
    c = {"source_holder": "hub", "state": "published", "manifest_revision": 1, "attached_run_uid": None,
         "reason": None, "updated_at": 1}
    c.update(over)
    return c


def enabled(**over) -> kws.Settings:
    base = dict(enabled=True, requested=True, clones={}, publish_on_park=True, push_timeout_s=30,
                preflight_timeout_s=60, materialize_timeout_s=None)
    base.update(over)
    return kws.Settings(**base)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def conn(kanban_home):
    yield from support.synced_conn(kanban_home)


# --- manifest (v2 §1, v4 §0) ----------------------------------------------------------------------------------------

def test_manifest_v1_parses():
    m = kws.parse_manifest(json.dumps(manifest()))
    assert (m.repository_id, m.short_branch, m.commit, m.manifest_revision) == (42, "wt/t_1", C1, 1)


@pytest.mark.parametrize("bad", [
    {"version": 2}, {"extra": 1}, {"artifacts": [{"attachment_uid": "u", "filename": "f", "sha256": "0" * 64}]},
    {"commit": "HEAD"}, {"branch": "wt/t_1"}, {"branch": "refs/heads/../x"}, {"repository": {"id": 1}},
    {"repository": {"id": 42, "full_name": "acme/widget", "url": "https://gitlab.com/acme/widget"}},
    {"manifest_revision": 0}, {"published_by": {"run_uid": "r1"}}, {"source_kind": "dir"},
    {"base_commit": "xyz"}, {"version": True},
])
def test_manifest_strict_schema_refuses(bad):
    with pytest.raises(kws.ManifestInvalid):
        kws.parse_manifest(manifest(**bad))


def test_manifest_size_cap():
    with pytest.raises(kws.ManifestInvalid, match="max"):
        kws.parse_manifest(json.dumps(manifest()) + " " * kws.MANIFEST_MAX_BYTES)


# --- settings (S9) -----------------------------------------------------------------------------------------------

def test_enabled_honoured_once_protocol_complete(caplog):
    """PR-2 completes the protocol: ``enabled: true`` now enables (no refusal warning); default stays off."""
    kws._WARNED.clear()
    with caplog.at_level(logging.WARNING):
        s = kws.settings({"kanban": {"workspace_source": {"enabled": True, "clones": {"acme/widget": "/x"}}}})
    assert s.requested and s.enabled
    assert "SOURCE_PROTOCOL_COMPLETE" not in caplog.text
    assert kws.SOURCE_PROTOCOL_COMPLETE is True
    assert not kws.settings({"kanban": {}}).enabled


def test_config_defaults_and_derived_timeouts():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["workspace_source"] == {
        "enabled": False, "clones": {}, "publish_on_park": True, "push_timeout_s": 30,
        "preflight_timeout_s": None, "materialize_timeout_s": None}
    s = kws.settings({"kanban": {"workspace_source": {"push_timeout_s": 10}}})
    assert (s.enabled, s.preflight_timeout_s, s.materialize_bound(900)) == (False, 40, 270)


# --- grant snapshot (v3 §6, v4.1 §B S2) -------------------------------------------------------------------------

def test_snapshot_keys_absent_is_old_board():
    assert kws.snapshot_from_row({"title": "x"}) is None
    assert kws.snapshot_from_row(None) is None


def test_snapshot_present_null_is_no_custody():
    snap = kws.snapshot_from_row({"workspace_source": None, "workspace_custody": None})
    assert snap is not None and not snap.has_source


def test_grant_snapshot_threads_from_lease_response(conn):
    """``HubLeaseClient._lease`` reads the grant response row; ``stage_claim`` hands it to the dispatcher."""
    hub = SimpleNamespace(leases=lambda board_id, body: {
        "status": "applied", "lease": {"run_uid": body["run_uid"], "expires": 10**13, "holder": "gw"}, "version": 3,
        "row": {"workspace_source": manifest(), "workspace_custody": custody()}})
    lease = ksc.HubLeaseClient(hub).acquire(board_id="b", task_uid="t_x", run_uid="r9", gateway_id="gw", ttl_s=60)
    assert lease.granted and lease.source_snapshot.has_source
    assert lease.source_snapshot.manifest().commit == C1
    tid = kb.create_task(conn, title="snap", assignee="worker")
    gate = sync.ClaimGate(10**10, lease=lease, ids=("b", "gw", tid))
    with kbc.write_txn(conn):
        sync.stage_claim(conn, tid, gate)
    assert kws.pop_grant_snapshot(tid) is lease.source_snapshot
    old = ksc.HubLeaseClient(SimpleNamespace(leases=lambda b, body: {
        "status": "applied", "lease": {"run_uid": body["run_uid"], "expires": 1}})).acquire(
        board_id="b", task_uid="t_x", run_uid="r9", gateway_id="gw", ttl_s=60)
    assert old.source_snapshot is None


# --- pull-only columns (v3 §11.2) ---------------------------------------------------------------------------------

def _with_source(conn, tid, src=None, cust=None):
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute("UPDATE tasks SET workspace_source = ?, workspace_custody = ? WHERE id = ?",
                     (None if src is None else json.dumps(src), None if cust is None else json.dumps(cust), tid))
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def test_pull_only_columns_applied_never_captured(conn):
    cols_now = set(sync.table_columns(conn, "tasks"))
    assert {"workspace_source", "workspace_custody"} <= cols_now
    assert not {"workspace_source", "workspace_custody"} & set(sync.captured_columns(conn, "tasks"))
    assert not {"workspace_source", "workspace_custody"} & sync.SYNCED_COLUMNS["tasks"]
    assert not sync.undeclared_columns(conn).get("tasks")
    tid = kb.create_task(conn, title="pulled", assignee="worker")
    cols = sync._local_fields(conn, "tasks", {"workspace_source": manifest(), "workspace_custody": custody(),
                                              "title": "pulled"}, tid)
    assert json.loads(cols["workspace_source"]) == manifest() and json.loads(cols["workspace_custody"]) == custody()
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    _with_source(conn, tid, manifest(), custody())
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = 2 WHERE id = ?", (tid,))
    ops = [json.loads(r[0]) for r in conn.execute("SELECT fields FROM kanban_sync_outbox WHERE row_uid = ?", (tid,))]
    assert ops and not any({"workspace_source", "workspace_custody"} & set(op) for op in ops)


def test_row_fields_pin_accepts_virtual_receipt():
    """The Board row-contract pin (``kanban_board_row_fields.json``) carries the hub PR's ``source_receipt``."""
    from pathlib import Path
    pin = json.loads(Path(sync.__file__).with_name("kanban_board_row_fields.json").read_text(encoding="utf-8"))
    for table, fields in sync.VIRTUAL_WIRE_FIELDS.items():
        assert set(fields) <= set(pin["row_fields"][table])
    assert not {"workspace_source", "workspace_custody"} & set(pin["row_fields"]["tasks"])  # never row-writable


# --- R4c fence + compat ---------------------------------------------------------------------------------------------

def test_edit_kind_refused_on_custody_card(conn):
    from hermes_cli import kanban_db_workspace as kbw
    tid = kb.create_task(conn, title="fenced", assignee="worker")
    kbw.set_branch_name(conn, tid, "wt/a")  # no source yet: allowed
    kind = conn.execute("SELECT workspace_kind FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    _with_source(conn, tid, cust=custody(state="retained"))
    with pytest.raises(kws.SourceFenced, match="source_fenced"):
        kbw._set_task_column(conn, tid, "workspace_kind", "dir")
    with pytest.raises(kws.SourceFenced):
        kbw.set_branch_name(conn, tid, "wt/b")
    kbw.set_branch_name(conn, tid, "wt/a")  # unchanged value: not a flip
    kbw.set_workspace_path(conn, tid, "/tmp/elsewhere")  # not fenced
    assert tuple(conn.execute("SELECT workspace_kind, branch_name FROM tasks WHERE id = ?", (tid,)).fetchone()) == (
        kind, "wt/a")


@pytest.mark.parametrize("field", ["workspace_kind", "branch_name"])
def test_fenced_field_refusal_strips_and_resends_without_omission(conn, field):
    """Q3/R4c: ``not writable on tasks: <field>`` for a fenced column drops it, resends the rest, no omission."""
    from hermes_cli import kanban_sync_compat as compat
    tid = kb.create_task(conn, title="flip", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
        conn.execute(f"UPDATE tasks SET {field} = 'dir', priority = 5 WHERE id = ?", (tid,))
    row = dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE row_uid = ? AND op = 'upsert'", (tid,)).fetchone())
    wire = ksc.translate(ksc._full_row_if_unseen(conn, row), "hlc")
    with kbc.write_txn(conn):
        out = compat.enqueue_retry(conn, row, wire, {"reason": "forbidden_field",
                                                     "detail": f"not writable on tasks: {field}"})
    assert out == compat.VIRTUAL_DROPPED
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_omissions").fetchone()[0] == 0
    resent = [json.loads(r[0]) for r in conn.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE row_uid = ? AND op_id != ?", (tid, row["op_id"]))]
    assert resent and all(field not in f for f in resent) and any(f.get("priority") == 5 for f in resent)


# --- R4b routing ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["scratch", "dir", "worktree"])
def test_custody_card_routes_to_source_resolver_regardless_of_kind(conn, monkeypatch, kind):
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli import kanban_source_dispatch as ksd
    from hermes_cli import kanban_source_resolver as ksr

    def legacy(*_a, **_k):
        raise AssertionError("legacy resolver reached for a source card")
    for name in ("resolve_worktree_for_dispatch", "resolve_workspace", "_ensure_git_worktree"):
        monkeypatch.setattr(kbw, name, legacy)
    seen = {}

    def fake(conn_, task, snapshot, **kw):
        seen.update(task=task.id, snapshot=snapshot)
        return ksr.ResolvedSource(path="/src/wt", branch="wt/t_1", commit=C1, manifest_revision=1, mode="materialized")
    monkeypatch.setattr(ksr, "resolve_source_workspace", fake)
    monkeypatch.setattr(kws, "settings", lambda config=None: enabled())
    claimed = SimpleNamespace(id="t_route", workspace_kind=kind, claim_expires=None, current_run_id=None)
    snap = kws.snapshot_from_row({"workspace_source": manifest(), "workspace_custody": custody()})
    kws.stash_grant_snapshot("t_route", snap)
    workspace, branch, source = ksd.resolve(conn, claimed, board=None, kbw=kbw)
    assert (str(workspace), branch, source.mode, seen["snapshot"]) == ("/src/wt", "wt/t_1", "materialized", snap)


def test_no_source_snapshot_takes_legacy_path(conn, monkeypatch):
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli import kanban_source_dispatch as ksd
    monkeypatch.setattr(kbw, "resolve_workspace", lambda task, board=None: "/legacy")
    claimed = SimpleNamespace(id="t_plain", workspace_kind="scratch")
    kws.stash_grant_snapshot("t_plain", kws.snapshot_from_row({"workspace_source": None, "workspace_custody": None}))
    assert ksd.resolve(conn, claimed, board=None, kbw=kbw) == ("/legacy", None, None)


def test_kind_flip_does_not_readmit_creator(conn, monkeypatch):
    """A card retained on another host whose local ``workspace_kind`` was flipped to ``dir`` still routes on the
    grant snapshot, so the creator never falls back to its legacy ``dir``/worktree path."""
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli import kanban_source_dispatch as ksd
    monkeypatch.setattr(kbw, "resolve_workspace", lambda *a, **k: pytest.fail("legacy dir path reached"))
    monkeypatch.setattr(kws, "settings", lambda config=None: enabled())
    claimed = SimpleNamespace(id="t_flip", workspace_kind="dir", claim_expires=None, workspace_path=None)
    kws.stash_grant_snapshot("t_flip", kws.snapshot_from_row(
        {"workspace_source": manifest(), "workspace_custody": custody(source_holder="worker-2", state="retained")}))
    with pytest.raises(kws.WorkspaceSourceRefusal) as err:
        ksd.resolve(conn, claimed, board=None, kbw=kbw)
    assert (err.value.code, err.value.scope) == ("custody_not_portable", "host")


@pytest.mark.parametrize("kind", ["scratch", "dir", "worktree"])
def test_dispatch_routes_before_kind_branch(conn, monkeypatch, kind):
    """Exercise the real lane dispatcher through resolution and spawn, not its source text."""
    from hermes_cli import kanban_db_dispatch as d
    from hermes_cli import kanban_db_dispatch_admission as admission
    from hermes_cli import kanban_source_resolver as resolver
    monkeypatch.setattr(admission, "lane_admission_refused", lambda *a, **kw: False)
    monkeypatch.setattr("hermes_cli.kanban_model_assignment.dispatch_guard", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.kanban_owner_down.preflight", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.kanban_model_tier.apply_model_tier", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **kw: None)
    tid = kb.create_task(conn, title="route", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind=? WHERE id=?", (kind, tid))
    snapshot = kws.SourceSnapshot(manifest(), kws.Custody("hub", "published", 1))

    def claim(conn_, task_id, *args):
        assert kb.claim_task(conn_, task_id, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
        kws.stash_grant_snapshot(task_id, snapshot)
        return kb.get_task(conn_, task_id)
    monkeypatch.setattr(admission, "admitted_claim", claim)
    for name in ("resolve_workspace", "resolve_worktree_for_dispatch", "_ensure_git_worktree"):
        monkeypatch.setattr(d._kbw, name, lambda *a, **kw: pytest.fail("legacy kind resolver/writer reached"))
    branches = []
    monkeypatch.setattr(d._kbw, "set_branch_name", lambda conn_, task_id, b: branches.append(b))
    seen = []

    def source(conn_, task_, snap, **kw):
        assert snap is snapshot
        seen.append(task_.id)
        return resolver.ResolvedSource("/source/exact", "wt/exact", C1, 1, "materialized")
    monkeypatch.setattr(resolver, "resolve_source_workspace", source)
    monkeypatch.setattr(kws, "settings", lambda config=None: enabled())
    spawned = []
    result = d.DispatchResult()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert d._dispatch_lane_task(conn, row, "worker", result, lane="ready", dry_run=False,
        ttl_seconds=900, board=None, failure_limit=2,
        spawn_fn=lambda task, path: spawned.append((task.id, path, task.branch_name)),
        per_profile_cap=None, per_profile_running={})
    assert seen == [tid] and spawned == [(tid, "/source/exact", "wt/exact")]
    assert result.spawned == [(tid, "worker", "/source/exact")]
    assert kb.get_task(conn, tid).workspace_path == "/source/exact"
    # The worker gets the manifest's (source) branch, never the legacy default, and the fenced branch_name column is
    # never written for a source card (the write raised SourceFenced on a custody replica: LIVE t_01a1d5b1c1798e2a).
    assert branches == []


# --- pickup skip + skip memo ----------------------------------------------------------------------------------------

def _card(**cols):
    return {"id": "t_pick", "workspace_source": None, "workspace_custody": None, **cols}


def test_pickup_skip_disabled_is_inert(conn):
    card = _card(workspace_source=json.dumps(manifest()), workspace_custody=json.dumps(custody(state="retained")))
    assert kws.pickup_skip(conn, card, "worker-2", cfg=kws.settings({})) is None


@pytest.mark.parametrize("cols,proven,expect", [
    ({}, set(), None),  # plain card
    ({"workspace_custody": json.dumps(custody(state="retained"))}, {42}, "manifest_invalid"),
    ({"workspace_source": json.dumps(manifest()), "workspace_custody": json.dumps(custody(state="retained"))}, {42},
     "custody retained"),
    ({"workspace_source": json.dumps(manifest(version=2)), "workspace_custody": json.dumps(custody())}, {42},
     "manifest_invalid"),
    ({"workspace_source": json.dumps(manifest()), "workspace_custody": json.dumps(custody())}, set(), "no proven clone"),
    ({"workspace_source": json.dumps(manifest()), "workspace_custody": json.dumps(custody())}, {42}, None),
    ({"workspace_source": json.dumps(manifest()),
      "workspace_custody": json.dumps(custody(source_holder="worker-2", state="retained"))}, set(), None),  # holder
])
def test_pickup_skip(conn, cols, proven, expect):
    out = kws.pickup_skip(conn, _card(**cols), "worker-2", cfg=enabled(), proven_ids=proven)
    assert (out is None) if expect is None else (out is not None and expect in out)


def test_worker_node_pickup_skips_unportable_source_card(conn, monkeypatch):
    """The pickup queue leaves a source card it cannot materialize on the Board (inert while disabled)."""
    from hermes_cli import kanban_worker_node as kwn
    import hermes_cli.kanban_runs_on_capabilities as caps
    monkeypatch.setattr(kwn, "eligible", lambda card, own, machines: None)
    monkeypatch.setattr(caps, "claim_refusal", lambda card, me=None: None)
    card = {"id": "t_board", "status": "ready", "runs_on": '["linux"]', "assignee": None,
            "workspace_source": json.dumps(manifest()), "workspace_custody": json.dumps(custody())}
    args = dict(mode="auto", review_enabled=True, profile_exists=None)
    assert kwn._skip_reason(conn, card, "worker-2", {}, **args) is None  # disabled: nothing changes
    monkeypatch.setattr(kws, "settings", lambda config=None: enabled())
    monkeypatch.setattr("hermes_cli.kanban_source_clones.proven_repo_ids", lambda cfg=None: set())
    reason, detail = kwn._skip_reason(conn, card, "worker-2", {}, **args)
    assert reason == "capability_missing" and "no proven clone of acme/widget" in detail


def test_pickup_proofs_are_batched_and_settings_readonly(conn, monkeypatch):
    from hermes_cli import kanban_source_clones as kcl
    from hermes_cli import config
    calls = []
    monkeypatch.setattr(config, "load_config", lambda: pytest.fail("hot-path config deepcopy"))
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"workspace_source": {"enabled": True}}})
    monkeypatch.setattr(kws, "SOURCE_PROTOCOL_COMPLETE", True)
    assert kws.settings().enabled
    monkeypatch.setattr(kcl, "proven_repo_ids", lambda cfg: calls.append(cfg) or {42})
    card = _card(workspace_source=json.dumps(manifest()), workspace_custody=json.dumps(custody()))
    with kws.pickup_batch():
        assert kws.pickup_skip(conn, card, "worker-2") is None
        assert kws.pickup_skip(conn, {**card, "id": "another"}, "worker-2") is None
    assert len(calls) == 1
    # Each new scan re-proves; nothing cached across ticks/heartbeats or resolver admission.
    with kws.pickup_batch():
        assert kws.pickup_skip(conn, card, "worker-2") is None
    assert len(calls) == 2


def test_sync_round_attests_sources(monkeypatch):
    """``sync_all_once`` runs the (rate-limited, inert-when-disabled) attestation hook every round."""
    from hermes_cli import kanban_source_clones as kcl
    calls = []
    monkeypatch.setattr(kcl, "attest_if_due", lambda hub: calls.append(hub))
    monkeypatch.setattr(ksc, "publish_pickup_if_changed", lambda hub: False)
    hub = SimpleNamespace(ident=None)
    ksc.sync_all_once(hub, boards=[])
    assert calls == [hub]


def test_host_refusal_memo_skips_revision_until_republish(conn):
    tid = kb.create_task(conn, title="memo", assignee="worker")
    _with_source(conn, tid, src=manifest(manifest_revision=3), cust=custody(manifest_revision=3))
    with kbc.write_txn(conn):
        kws.remember_skip(conn, tid, 3, "materialize_timeout")
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert sync._source_skip(conn, row) == "workspace_source_refused_here:materialize_timeout"
    assert "materialize_timeout" in kws.pickup_skip(conn, dict(row), "worker-2", cfg=enabled(), proven_ids={42})
    _with_source(conn, tid, src=manifest(manifest_revision=4), cust=custody(manifest_revision=4))
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert sync._source_skip(conn, row) is None


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("feed_order", ["task_first", "run_first"])
@pytest.mark.parametrize("code", ["materialize_timeout", "remote_drift"])
def test_primary_refusal_returns_unused_grant_without_competing_status(conn, code, feed_order, lane):
    from hermes_cli import kanban_source_dispatch as ksd
    tid = kb.create_task(conn, title="refusal", assignee="worker")
    if lane == "review":
        assert kb.request_review(conn, tid, summary="ready for review")
    claim = kb.claim_review_task if lane == "review" else kb.claim_task
    assert claim(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    claimed = kb.get_task(conn, tid)
    assert claimed is not None
    sync.set_mode(conn, "primary")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    result = SimpleNamespace(auto_blocked=[])
    assert ksd.refused(conn, claimed, kws.WorkspaceSourceRefusal(code, "refused"), result)
    ops = [dict(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='tasks'")]
    assert [(r["op"], json.loads(r["fields"])) for r in ops] == [("release", {
        "run_uid": sync.run_uid(conn, claimed.current_run_id), "outcome": "released", "unused_grant": True,
        "error": f"workspace_source: {code}: refused"})]
    assert kb.get_task(conn, tid).status == "running"  # feed, not local status writes, ends this run
    assert result.auto_blocked == []
    if code == "remote_drift":
        # ACK alone is not an ended run; only its authoritative feed may unblock the follow-up.
        sync.apply_remote_changes(conn, [], new_cursor=1)
        assert kb.get_task(conn, tid).status == "running"
        uid = sync.run_uid(conn, claimed.current_run_id)
        changes = [
            sync.RemoteChange(seq=1, table="tasks", uid=tid, op="upsert", fields={"status": lane, "current_run_uid": None,
                              "lease_holder": None, "lease_expires": None}, version=2, hlc="", origin_gateway="board"),
            sync.RemoteChange(seq=2, table="task_runs", uid=uid, op="upsert", fields={"task_id": tid, "status": "released",
                              "outcome": "released", "ended_at": 2000, "metadata": json.dumps({"unused_grant": True})},
                              version=2, hlc="", origin_gateway="board"),
        ]
        if feed_order == "run_first":
            changes.reverse()
        sync.apply_remote_changes(conn, changes[:1], new_cursor=2)
        assert kb.get_task(conn, tid).status != "blocked"
        sync.apply_remote_changes(conn, changes[1:], new_cursor=3)
        assert kb.get_task(conn, tid).status == "blocked"
        sync.apply_remote_changes(conn, [], new_cursor=3)
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()[0] == 1
        task = kb.get_task(conn, tid)
        assert task.block_kind == "needs_input" and task.human_request
        ended = conn.execute("SELECT outcome, metadata FROM task_runs WHERE id=?", (claimed.current_run_id,)).fetchone()
        assert ended["outcome"] == "released" and json.loads(ended["metadata"])["unused_grant"] is True
        pending = [dict(r) for r in conn.execute(
            "SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND op='upsert' ORDER BY rowid")]
        wire = [ksc.translate(ksc._full_row_if_unseen(conn, r), "hlc") for r in pending]
        assert any(op["fields"].get("status") == "blocked" for op in wire)
        assert all(op["op"] == "upsert" for op in wire)  # Board rows.ts, NOT TASK_OPS.block
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == lane


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("superseded", [False, True])
def test_refusal_block_ignores_lost_release_or_newer_run(conn, superseded, lane):
    from hermes_cli import kanban_source_dispatch as ksd
    from hermes_cli.kanban_source_refusal_block import drain
    tid = kb.create_task(conn, title="superseded refusal", assignee="worker")
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    claimed = kb.get_task(conn, tid)
    sync.set_mode(conn, "primary")
    assert ksd.refused(conn, claimed, kws.WorkspaceSourceRefusal("remote_drift"), SimpleNamespace(auto_blocked=[]))
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET status=?, current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?", (lane, tid))
        conn.execute("UPDATE task_runs SET ended_at=100, outcome=?, metadata=? WHERE id=?", (
            "released" if superseded else "expired", json.dumps({"unused_grant": True}), claimed.current_run_id))
        if superseded:
            conn.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome) VALUES (?, 'done',101,102,'completed')", (tid,))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
    drain(conn)
    assert kb.get_task(conn, tid).status == lane
    assert conn.execute("SELECT COUNT(*) FROM kanban_source_blocks").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["bad_json", "block_bug"])
def test_followup_failure_does_not_poison_committed_feed(conn, monkeypatch, caplog, failure):
    from hermes_cli import kanban_source_refusal_block as followup
    tid = kb.create_task(conn, title="drain failure", assignee="worker")
    assert kb.claim_task(conn, tid)
    claimed = kb.get_task(conn, tid)
    assert claimed is not None
    if failure == "block_bug":
        def broken_block(*args, **kwargs):
            raise AttributeError("injected block failure")
        monkeypatch.setattr(followup, "block", broken_block)
    with kbc.write_txn(conn):
        followup.defer(conn, claimed, kws.WorkspaceSourceRefusal("remote_drift"))
        if failure == "bad_json":
            conn.execute("UPDATE kanban_source_blocks SET refusal='not json'")
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, lease_holder=NULL WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET ended_at=100, outcome='released', metadata=? WHERE id=?",
                     (json.dumps({"unused_grant": True}), claimed.current_run_id))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
    for cursor in (101, 102):
        result = sync.apply_remote_changes(conn, [], new_cursor=cursor)
        assert result.cursor == cursor
        assert conn.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == cursor
    assert conn.execute("SELECT COUNT(*) FROM kanban_source_blocks").fetchone()[0] == 1
    assert "source refusal follow-up" in caplog.text


@pytest.mark.parametrize("missing", ["task", "run"])
def test_orphan_followups_expire_but_wait_for_separate_feed_pages(conn, monkeypatch, missing):
    from hermes_cli import kanban_source_refusal_block as followup
    import time
    now = time.time()
    monkeypatch.setattr(followup, "time", SimpleNamespace(time=lambda: now), raising=False)
    tid = kb.create_task(conn, title="orphan", assignee="worker")
    assert kb.claim_task(conn, tid)
    claimed = kb.get_task(conn, tid)
    with kbc.write_txn(conn):
        followup.defer(conn, claimed, kws.WorkspaceSourceRefusal("remote_drift"))
        conn.execute("UPDATE kanban_source_blocks SET " + (
            "run_uid='run-not-arrived'" if missing == "run" else "task_id='task-not-arrived'"))
    followup.drain(conn)
    assert conn.execute("SELECT COUNT(*) FROM kanban_source_blocks").fetchone()[0] == 1
    now += 8 * 24 * 3600
    followup.drain(conn)
    assert conn.execute("SELECT COUNT(*) FROM kanban_source_blocks").fetchone()[0] == 0


def test_legacy_followup_schema_gets_full_orphan_retention(conn, monkeypatch):
    from hermes_cli import kanban_source_refusal_block as followup
    now = 1000000
    monkeypatch.setattr(followup, "time", SimpleNamespace(time=lambda: now))
    with kbc.write_txn(conn):
        conn.execute("CREATE TABLE kanban_source_blocks (run_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL, refusal TEXT NOT NULL)")
        conn.execute("INSERT INTO kanban_source_blocks VALUES ('missing-run', 'missing-task', '{}')")
    followup.drain(conn)
    assert conn.execute("SELECT created_at FROM kanban_source_blocks").fetchone()[0] == now
    now += followup.ORPHAN_RETENTION_S + 1
    followup.drain(conn)
    assert conn.execute("SELECT COUNT(*) FROM kanban_source_blocks").fetchone()[0] == 0


def test_followup_review_reblock_reaches_triage_and_keeps_review_resume(conn):
    """Same-cause accounting (route_block): unblock -> re-refuse of a review card climbs to triage; each
    unblock below the limit resumes in review."""
    from hermes_cli.kanban_source_refusal_block import _block_released_review
    tid = kb.create_task(conn, title="review loop", assignee="worker")
    assert kb.request_review(conn, tid, summary="ready")
    statuses = []
    for n in range(kb.BLOCK_RECURRENCE_LIMIT):
        assert kb.claim_review_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
        run_id = kb.get_task(conn, tid).current_run_id
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review', current_run_id=NULL, claim_lock=NULL WHERE id=?", (tid,))
            conn.execute("UPDATE task_runs SET ended_at=?, outcome='released' WHERE id=?", (100 + n, run_id))
        uid = sync.run_uid(conn, run_id)
        row = {"task_id": tid, "refused_run_id": run_id, "run_uid": uid}
        assert _block_released_review(conn, row, kws.WorkspaceSourceRefusal("remote_drift", "moved"))
        statuses.append(kb.get_task(conn, tid).status)
        if statuses[-1] == "blocked":
            assert kb.unblock_task(conn, tid)
            assert kb.get_task(conn, tid).status == "review"
    assert statuses == ["blocked"] * (kb.BLOCK_RECURRENCE_LIMIT - 1) + ["triage"]
    assert kb.get_task(conn, tid).block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
    assert kinds.count("block_loop_detected") == 1


def test_followup_failing_forever_is_quarantined_with_evidence(conn, caplog):
    from hermes_cli import kanban_source_refusal_block as followup
    tid = kb.create_task(conn, title="poison", assignee="worker")
    assert kb.claim_task(conn, tid)
    claimed = kb.get_task(conn, tid)
    with kbc.write_txn(conn):
        followup.defer(conn, claimed, kws.WorkspaceSourceRefusal("remote_drift"))
        conn.execute("UPDATE kanban_source_blocks SET refusal='not json'")
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, lease_holder=NULL WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET ended_at=100, outcome='released', metadata=? WHERE id=?",
                     (json.dumps({"unused_grant": True}), claimed.current_run_id))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
    for _ in range(followup.MAX_FOLLOWUP_FAILURES):
        followup.drain(conn)
    assert conn.execute("SELECT COUNT(*) FROM kanban_source_blocks").fetchone()[0] == 0
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                      (tid, followup.QUARANTINED_EVENT)).fetchone()
    payload = json.loads(ev[0])
    assert payload["refusal"] == "not json" and payload["failures"] == followup.MAX_FOLLOWUP_FAILURES
    caplog.clear()
    followup.drain(conn)  # a later feed apply logs nothing more
    assert "source refusal follow-up" not in caplog.text


def test_followup_drain_opens_no_write_txn_when_empty(conn, monkeypatch):
    from hermes_cli import kanban_source_refusal_block as followup
    with kbc.write_txn(conn):
        followup._ensure_schema(conn)
    monkeypatch.setattr(followup.kb, "write_txn", lambda c: pytest.fail("write txn opened for an empty table"))
    followup.drain(conn)


def test_resolve_refuses_source_card_when_disabled(conn, monkeypatch):
    """Worker-side gate: a disabled host never reaches the source resolver, whatever the Board granted."""
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli import kanban_source_dispatch as ksd
    from hermes_cli import kanban_source_resolver as ksr
    monkeypatch.setattr(kws, "settings", lambda config=None: enabled(enabled=False))
    monkeypatch.setattr(ksr, "resolve_source_workspace", lambda *a, **k: pytest.fail("resolver reached"))
    claimed = SimpleNamespace(id="t_off", workspace_kind="scratch", claim_expires=None)
    kws.stash_grant_snapshot("t_off", kws.snapshot_from_row({"workspace_source": manifest(),
                                                            "workspace_custody": custody()}))
    with pytest.raises(kws.WorkspaceSourceRefusal) as err:
        ksd.resolve(conn, claimed, board=None, kbw=kbw)
    assert (err.value.code, err.value.scope, err.value.evidence["manifest_revision"]) == ("source_disabled", "host", 1)


@pytest.mark.parametrize("mode", ["materialized", "holder_reuse"])
def test_materialized_event_names_current_holder_and_grant_source(conn, mode):
    from hermes_cli import kanban_source_dispatch as dispatch
    from hermes_cli.kanban_source_resolver import ResolvedSource
    tid = kb.create_task(conn, title="source event", assignee="worker")
    assert kb.claim_task(conn, tid)
    claimed = kb.get_task(conn, tid)
    assert claimed is not None
    own = dispatch._me(conn)
    previous = "previous-worker" if mode == "materialized" else own
    snapshot = kws.SourceSnapshot(manifest(manifest_revision=7), kws.Custody(previous, "published", 7))
    # A reused local copy may carry only its path/branch; the grant owns publication metadata.
    source = ResolvedSource("/source/task", "wt/t_1", None, None, mode)
    dispatch.materialized_event(conn, claimed, snapshot, source)
    payload = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='workspace_source_materialized'", (tid,)
    ).fetchone()[0])
    assert payload["holder"] == own
    assert payload["prev_holder"] == previous
    assert {key: payload[key] for key in ("repository", "commit", "manifest_revision", "source_kind")} == {
        "repository": "acme/widget", "commit": C1, "manifest_revision": 7, "source_kind": "task_worktree"}
    assert payload["run_uid"] == sync.run_uid(conn, claimed.current_run_id)
    assert payload["path"] == source.path and payload["mode"] == mode


@pytest.mark.parametrize("snapshot_kind", ["no_snapshot", "no_custody", "custody_only"])
def test_materialized_event_without_manifest_keeps_known_metadata(conn, snapshot_kind):
    from hermes_cli import kanban_source_dispatch as dispatch
    from hermes_cli.kanban_source_resolver import ResolvedSource
    tid = kb.create_task(conn, title="unpublished source", assignee="worker")
    custody_value = kws.Custody("previous", "retained", 3) if snapshot_kind == "custody_only" else None
    snapshot = None if snapshot_kind == "no_snapshot" else kws.SourceSnapshot(None, custody_value)
    source = ResolvedSource("/source/task", "wt/t_1", None, None, "holder_reuse")
    dispatch.materialized_event(conn, SimpleNamespace(id=tid), snapshot, source)
    payload = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='workspace_source_materialized'", (tid,)
    ).fetchone()[0])
    assert payload["holder"] == dispatch._me(conn)
    assert payload["prev_holder"] == ("previous" if custody_value else None)
    assert payload["manifest_revision"] == (3 if custody_value else None)
    assert payload["repository"] is None and payload["commit"] is None and payload["source_kind"] is None
    assert payload["run_uid"] is None


def test_refusal_codes_scoped():
    assert kws.WorkspaceSourceRefusal("materialize_timeout").scope == "host"  # Q3
    for code in ("remote_drift", "remote_branch_missing", "manifest_invalid", "custody_source_missing"):
        assert kws.WorkspaceSourceRefusal(code).scope == "card"
    with pytest.raises(ValueError):
        kws.WorkspaceSourceRefusal("made_up")
    from hermes_cli.kanban_db_workspace import WorktreeWorkspaceConfigError
    assert not issubclass(kws.WorkspaceSourceRefusal, WorktreeWorkspaceConfigError)
