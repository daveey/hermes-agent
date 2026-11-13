"""workspace_source core PR-2: source_attach before spawn, the pre-spawn abort (R2-a) and per-call source_protocol
(R3-a), driven through the real lane dispatcher (plan v3 §6, v4 §2.2, v4.1 R2-a/R3-a/S2)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_board as kbs
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_workspace_source as kws
from tests.hermes_cli import kanban_source_support as fake
from tests.hermes_cli import kanban_sync_support as support

C1 = "c" * 40


def manifest(**over) -> dict:
    m = {"version": 1, "repository": {"id": 42, "full_name": "acme/widget", "url": "https://github.com/acme/widget"},
         "branch": "refs/heads/wt/exact", "commit": C1, "base_commit": None, "published_at": 1,
         "manifest_revision": 3, "published_by": {"run_uid": "r1", "machine": "hub"}, "source_kind": "task_worktree",
         "artifacts": []}
    m.update(over)
    return m


def enabled() -> kws.Settings:
    return kws.Settings(enabled=True, requested=True, clones={}, publish_on_park=True, push_timeout_s=30,
                        preflight_timeout_s=60, materialize_timeout_s=None)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def conn(kanban_home):
    yield from support.synced_conn(kanban_home)


@pytest.fixture
def board(conn, monkeypatch):
    b = fake.FakeSourceBoard()
    previous = fake.install(b)
    sync.set_mode(conn, "primary")
    monkeypatch.setattr(kws, "settings", lambda config=None: enabled())
    yield b
    sync.set_lease_client(previous)
    kbs.reset_board_lacks_source()


@pytest.fixture
def dispatcher(conn, monkeypatch, tmp_path):
    """The real ``_dispatch_lane_task`` with admission/model gates opened and the resolver a double that returns a
    real directory (the attach observes it)."""
    from hermes_cli import kanban_db_dispatch as d
    from hermes_cli import kanban_db_dispatch_admission as admission
    from hermes_cli import kanban_source_resolver as resolver
    monkeypatch.setattr(admission, "lane_admission_refused", lambda *a, **kw: False)
    monkeypatch.setattr("hermes_cli.kanban_model_assignment.dispatch_guard", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.kanban_owner_down.preflight", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.kanban_model_tier.apply_model_tier", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", lambda *a, **kw: None)
    terminated = []
    monkeypatch.setattr(d, "_terminate_reclaimed_worker", lambda pid, lock=None: terminated.append(pid) or True)
    src = tmp_path / "src"
    src.mkdir()
    state = SimpleNamespace(snapshot=None, kind="worktree", terminated=terminated, path=src, order=[])

    def claim(conn_, task_id, *args):
        assert kb.claim_task(conn_, task_id, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
        kws.stash_grant_snapshot(task_id, state.snapshot)
        return kb.get_task(conn_, task_id)
    monkeypatch.setattr(admission, "admitted_claim", claim)

    def source(conn_, task_, snap, **kw):
        state.order.append("resolve")
        return resolver.ResolvedSource(str(src), "wt/exact", C1, 3, "materialized")
    monkeypatch.setattr(resolver, "resolve_source_workspace", source)
    monkeypatch.setattr(d._kbw, "resolve_worktree_for_dispatch", lambda *a, **kw: (src, "wt/legacy"))
    monkeypatch.setattr(d._kbw, "_maybe_emit_scratch_tip", lambda *a, **kw: None)

    def run(spawn_fn, *, kind="worktree", created_on=None, replica_custody=None):
        tid = kb.create_task(conn, title="src", assignee="worker")
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind=? WHERE id=?", (kind, tid))
            if created_on is not None:
                conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (created_on, tid))
            if replica_custody is not None:  # pulled from the Board: the replica carries custody, no branch_name
                conn.execute("UPDATE kanban_sync_state SET applying=1")
                conn.execute("UPDATE tasks SET workspace_custody=?, branch_name=NULL WHERE id=?",
                             (json.dumps(replica_custody), tid))
                conn.execute("UPDATE kanban_sync_state SET applying=0")
        result = d.DispatchResult()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        used = d._dispatch_lane_task(conn, row, "worker", result, lane="ready", dry_run=False, ttl_seconds=900,
                                     board=None, failure_limit=3, spawn_fn=spawn_fn, per_profile_cap=None,
                                     per_profile_running={})
        return tid, result, used
    state.run = run
    return state


def _source_snapshot():
    return kws.SourceSnapshot(json.dumps(manifest()), kws.Custody("hub", "published", 3))


def _outbox_releases(conn, tid):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? AND op='release'", (tid,))]


# --- attach before spawn --------------------------------------------------------------------------------------------

def test_source_attach_precedes_spawn_with_no_renew_between(conn, board, dispatcher):
    """Spy: attach is the Board call right before spawn; nothing renews the lease in between (v4.1 R2-a)."""
    dispatcher.snapshot = _source_snapshot()
    marks = []

    def spawn(task, path):
        marks.append(len(board.calls))
        assert task.source_env == {"HERMES_KANBAN_SOURCE_COMMIT": C1, "HERMES_KANBAN_SOURCE_PATH": str(dispatcher.path),
                                   "HERMES_KANBAN_SOURCE_BRANCH": "wt/exact"}
    tid, result, used = dispatcher.run(spawn)
    assert used and result.spawned and marks
    seen = board.actions()[:marks[0]]
    attach_at = seen.index("source_attach")
    assert "renew" not in seen[attach_at:]
    assert board.body("source_attach")["mode"] == "materialized"
    assert board.body("source_attach")["manifest_revision"] == 3
    run = kbs.attached_run(conn, kb.get_task(conn, tid).current_run_id)
    assert run is not None and run.state == "attached" and run.branch == "wt/exact"
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='workspace_source_materialized'",
                      (tid,)).fetchone()
    assert ev is not None
    assert set(json.loads(ev[0])) >= {"hostname", "holder", "run_uid", "repository", "branch", "commit",
                                      "manifest_revision", "path", "mode"}


def test_source_card_with_replica_custody_and_no_branch_spawns(conn, board, dispatcher):
    """LIVE t_01a1d5b1c1798e2a: the replica carries custody and branch_name is NULL (the Board refused the legacy
    branch write as forbidden_field). The launch must not write the fenced branch_name (SourceFenced escaped the tick
    and the lease went stale twice); the worker gets the source branch in memory and spawns."""
    dispatcher.snapshot = _source_snapshot()
    seen = []
    tid, result, used = dispatcher.run(lambda task, path: seen.append(task.branch_name),
                                       replica_custody={"source_holder": "hub", "state": "published",
                                                        "manifest_revision": 3})
    assert used and result.spawned and seen == ["wt/exact"]
    task = kb.get_task(conn, tid)
    assert task is not None and task.branch_name is None
    ops = [json.loads(r[0]) for r in conn.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (tid,))]
    assert [op["branch_name"] for op in ops if op.get("branch_name")] == []


def test_workspace_record_failure_releases_the_claim_instead_of_raising(conn, board, dispatcher, monkeypatch):
    """Any failure recording the workspace after the claim is released unused and counted; it never raises out of
    the dispatcher tick (which left the claim to expire as stale_lease and skipped the rest of the tick)."""
    from hermes_cli import kanban_db_dispatch as d

    def boom(*a, **kw):
        raise kws.SourceFenced("t: branch_name is fenced on a card with a workspace source (source_fenced)")
    monkeypatch.setattr(d._kbw, "set_workspace_path", boom)
    dispatcher.snapshot = _source_snapshot()
    spawned = []
    tid, result, used = dispatcher.run(lambda task, path: spawned.append(task.id))
    assert not used and spawned == []
    assert ("workspace_prepare_failed:SourceFenced" in [code for _, code in result.respawn_guarded])
    releases = _outbox_releases(conn, tid)
    assert len(releases) == 1 and releases[0].get("unused_grant") and "source_receipt" not in releases[0]
    assert "source_attach" not in board.actions()


def test_grant_carries_source_protocol_only_from_dispatcher(conn, board):
    tid = kb.create_task(conn, title="cli", assignee="worker")
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    assert "source_protocol" not in board.calls[-1][1]
    with kbs.protocol_call():
        assert kbs.grant_protocol() == 1
    assert kbs.grant_protocol() is None


def test_dispatcher_admitted_claim_acquires_with_source_protocol(conn, board, monkeypatch):
    """The real admitted_claim -> claim gate -> lease acquire path carries ``source_protocol: 1``."""
    from hermes_cli import kanban_db_dispatch as d
    from hermes_cli import kanban_db_dispatch_admission as admission
    monkeypatch.setattr("hermes_cli.kanban_capsule_admission.claim_refusal", lambda *a, **kw: None)
    tid = kb.create_task(conn, title="dispatched", assignee="worker")
    claimed = admission.admitted_claim(conn, tid, "worker", "ready", 900, lambda task, path: None,
                                       d.DispatchResult())
    assert claimed is not None and claimed.id == tid
    acquires = [b for k, b in board.calls if k == "acquire"]
    assert acquires and acquires[-1].get("source_protocol") == 1
    assert kbs.grant_protocol() is None  # the protocol never leaks past that one call


def test_attach_refused_releases_unused_grant_and_never_spawns(conn, board, dispatcher):
    dispatcher.snapshot = _source_snapshot()
    board.refuse["source_attach"] = "custody_attached"
    spawned = []
    _, result, used = dispatcher.run(lambda task, path: spawned.append(task.id))
    assert not used and spawned == []
    assert any(code.startswith("workspace_source_attach_refused:custody_attached") for _, code in result.respawn_guarded)


def test_legacy_adopt_by_creator(conn, board, dispatcher):
    """A legacy worktree card (no source on the Board) is adopted by its creator on its first enabled run."""
    dispatcher.snapshot = kws.SourceSnapshot(None, None)
    me = sync._state(conn)["gateway_id"]
    _, _, used = dispatcher.run(lambda task, path: None, created_on=me)
    assert used
    assert board.body("source_attach")["mode"] == "legacy_adopt"


def test_legacy_card_of_another_creator_is_not_adopted(conn, board, dispatcher):
    dispatcher.snapshot = kws.SourceSnapshot(None, None)
    dispatcher.run(lambda task, path: None, created_on="someone-else")
    assert "source_attach" not in board.actions()


def test_legacy_adopt_on_old_board_runs_without_custody(conn, board, dispatcher):
    dispatcher.snapshot = kws.SourceSnapshot(None, None)
    board.old = True
    me = sync._state(conn)["gateway_id"]
    _, _, used = dispatcher.run(lambda task, path: None, created_on=me)
    assert used and kbs.board_lacks_source()


# --- pre-spawn abort (R2-a) -----------------------------------------------------------------------------------------

def test_pre_spawn_abort_single_release(conn, board, dispatcher):
    """A spawn that never started a process: the receipt-carrying release goes first (unchanged + abort pre_spawn) and
    _record_task_failure adds no second release for the run."""
    dispatcher.snapshot = _source_snapshot()

    def spawn(task, path):
        raise launch.SpawnNotStarted("no slot")
    from tests.hermes_cli.test_kanban_source_resolver import git as _git
    path = dispatcher.path
    _git(path.parent, "init", "-q", str(path))
    _git(path, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "x")
    _git(path, "checkout", "-q", "-b", "wt/exact")
    head = _git(path, "rev-parse", "HEAD")
    dispatcher.snapshot = kws.SourceSnapshot(json.dumps(manifest(commit=head)), kws.Custody("hub", "published", 3))
    tid, _, used = dispatcher.run(spawn)
    assert not used
    releases = [b for k, b in board.calls if k == "release"]
    assert len(releases) == 1
    receipt = releases[0]["source_receipt"]
    assert (receipt["state"], receipt["abort"], receipt["commit"]) == ("unchanged", "pre_spawn", head)
    assert releases[0]["outcome"] == "spawn_failed"
    assert _outbox_releases(conn, tid) == []  # first wins per run_uid: no outbox duplicate


def test_pre_spawn_abort_with_changed_worktree_is_retained(conn, board, dispatcher):
    dispatcher.snapshot = _source_snapshot()  # the manifest commit is not what the directory holds

    def spawn(task, path):
        raise launch.SpawnNotStarted("no slot")
    dispatcher.run(spawn)
    receipt = board.body("release")["source_receipt"]
    assert receipt["state"] == "retained" and "abort" not in receipt


def test_pre_spawn_abort_outbox_fallback_carries_receipt(conn, board, dispatcher):
    dispatcher.snapshot = _source_snapshot()
    board.offline.add("release")

    def spawn(task, path):
        raise launch.SpawnNotStarted("no slot")
    tid, _, _ = dispatcher.run(spawn)
    releases = _outbox_releases(conn, tid)
    assert len(releases) == 1 and releases[0]["source_receipt"]["state"] == "retained"
    assert releases[0]["outcome"] == "spawn_failed"


def test_post_pid_failure_kills_worker_and_sends_retained(conn, board, dispatcher, monkeypatch):
    dispatcher.snapshot = _source_snapshot()

    def hook(*a, **kw):
        raise RuntimeError("hook exploded after the worker started")
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", hook)
    _, _, used = dispatcher.run(lambda task, path: 4242)
    assert not used
    assert dispatcher.terminated == [4242]
    receipt = board.body("release")["source_receipt"]
    assert receipt["state"] == "retained" and receipt["reason"] == "post_spawn_failure"


def _git_source(dispatcher):
    """Make the resolver's directory a real worktree on ``wt/exact``; returns its HEAD."""
    from tests.hermes_cli.test_kanban_source_resolver import git as _git
    path = dispatcher.path
    _git(path.parent, "init", "-q", str(path))
    _git(path, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "x")
    _git(path, "checkout", "-q", "-b", "wt/exact")
    return _git(path, "rev-parse", "HEAD")


def test_admission_refusal_after_attach_is_a_pre_spawn_abort(conn, board, dispatcher, monkeypatch):
    """A model/admission refusal raised after attach but before spawn (stage pre_spawn) never started a process:
    one receipt-carrying release (unchanged + abort pre_spawn), nothing spawned, no second release."""
    head = _git_source(dispatcher)
    dispatcher.snapshot = kws.SourceSnapshot(json.dumps(manifest(commit=head)), kws.Custody("hub", "published", 3))

    def refuse(*a, **kw):
        raise RuntimeError("no independent reviewer model is available")
    monkeypatch.setattr("hermes_cli.kanban_model_tier.apply_model_tier", refuse)
    spawned = []
    tid, _, used = dispatcher.run(lambda task, path: spawned.append(task.id))
    assert not used and spawned == []
    assert "source_attach" in board.actions()
    releases = [b for k, b in board.calls if k == "release"]
    assert len(releases) == 1
    receipt = releases[0]["source_receipt"]
    assert (receipt["state"], receipt["abort"], receipt["commit"]) == ("unchanged", "pre_spawn", head)
    assert _outbox_releases(conn, tid) == []


def test_old_board_attach_400_on_source_card_never_spawns(conn, board, dispatcher):
    """A source card whose grant carried a snapshot but whose attach meets an old Board (400 action list) must not
    run outside custody: the grant is released unused and nothing spawns."""
    dispatcher.snapshot = _source_snapshot()
    board.old = True
    spawned = []
    _, result, used = dispatcher.run(lambda task, path: spawned.append(task.id))
    assert not used and spawned == []
    assert any(code.startswith("workspace_source_attach_refused:board_lacks_source")
               for _, code in result.respawn_guarded)
    assert kbs.board_lacks_source()


def test_spawn_error_of_unknown_kind_is_not_trusted_as_not_started(conn, board, dispatcher):
    dispatcher.snapshot = _source_snapshot()

    def spawn(task, path):
        raise RuntimeError("Popen half-way")
    dispatcher.run(spawn)
    assert board.body("release")["source_receipt"]["state"] == "retained"


# --- S2: old Board -------------------------------------------------------------------------------------------------

def test_old_board_skips_card_with_replica_custody(conn, board, dispatcher):
    """A grant with no snapshot keys (old Board) for a card whose replica has custody refuses; never the legacy path."""
    dispatcher.snapshot = None
    spawned = []
    tid = kb.create_task(conn, title="has custody", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_source=?, workspace_custody=? WHERE id=?",
                     (json.dumps(manifest()), json.dumps({"source_holder": "hub", "state": "published",
                                                          "manifest_revision": 3}), tid))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
    from hermes_cli import kanban_db_dispatch as d
    result = d.DispatchResult()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    d._dispatch_lane_task(conn, row, "worker", result, lane="ready", dry_run=False, ttl_seconds=900, board=None,
                          failure_limit=3, spawn_fn=lambda task, path: spawned.append(task.id), per_profile_cap=None,
                          per_profile_running={})
    assert spawned == [] and kbs.board_lacks_source()


def _custody_card(conn, title):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_source=?, workspace_custody=? WHERE id=?",
                     (json.dumps(manifest()), json.dumps({"source_holder": "hub", "state": "published",
                                                          "manifest_revision": 3}), tid))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
    return tid


def test_old_board_skips_every_later_card_with_replica_custody(conn, board, dispatcher, monkeypatch):
    """Once this process knows the Board is old, EVERY later custody card is refused too (not only the first one
    that tripped the detection), and none reaches the legacy resolver."""
    from hermes_cli import kanban_db_dispatch as d
    dispatcher.snapshot = None
    legacy, spawned = [], []
    monkeypatch.setattr(d._kbw, "resolve_worktree_for_dispatch",
                        lambda *a, **kw: legacy.append(a[1].id) or (dispatcher.path, "wt/legacy"))
    for title in ("first", "second", "third"):
        tid = _custody_card(conn, title)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        d._dispatch_lane_task(conn, row, "worker", d.DispatchResult(), lane="ready", dry_run=False, ttl_seconds=900,
                              board=None, failure_limit=3, spawn_fn=lambda task, path: spawned.append(task.id),
                              per_profile_cap=None, per_profile_running={})
        refused = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='workspace_source_refused'",
                               (tid,)).fetchone()
        assert refused is not None and json.loads(refused[0])["code"] == "board_lacks_source", title
    assert spawned == [] and legacy == [] and kbs.board_lacks_source()
    assert launch._GRANTS == {}


def test_snapshot_present_null_is_no_custody():
    snap = kws.snapshot_from_row({"workspace_source": None, "workspace_custody": None})
    assert snap is not None and not snap.has_source
    assert kws.snapshot_from_row({"id": "t"}) is None
