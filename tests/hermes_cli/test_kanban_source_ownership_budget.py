"""PR #3307 review fixes: external source ownership, the materialize budget, and the host gate.

Real Git and SQLite; only the Board transport is the fake from ``kanban_source_support``.
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import replace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_source_registration as reg
from hermes_cli import kanban_source_resolver as resolver
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import test_kanban_source_dispatch_v2 as dispatch
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


def _source_calls(b):
    return [c for c in b.calls if c[0] not in ("acquire", "renew", "release")]


def _second_task(conn, tmp_path, branch):
    other = kb.create_task(conn, title="other source owner", assignee="worker")
    scratch = tmp_path / "scratch-other"
    scratch.mkdir()
    conn.execute("UPDATE tasks SET branch_name=?, workspace_kind='scratch', workspace_path=? WHERE id=?",
                 (branch, str(scratch), other))
    conn.commit()
    assert kb.claim_task(conn, other, ttl_seconds=900, claimer=f"{kb._host_prefix()}:2")
    return other


# --- P1: one external worktree has one live owner ---------------------------------------------------------------

def test_second_task_cannot_register_an_owned_external_worktree(setup, tmp_path):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    first = reg.register_initial(conn, tid, str(source))
    other = _second_task(conn, tmp_path, first.branch)
    with pytest.raises(launch.AttachRefused) as exc:
        reg.register_initial(conn, other, str(source))
    assert "another active task" in exc.value.detail
    assert board.attached_run(conn, kb.get_task(conn, other).current_run_id) is None
    assert board.attached_run(conn, kb.get_task(conn, tid).current_run_id).state == "attached"


def test_concurrent_registration_is_refused_inside_the_observation_transaction(setup, tmp_path, monkeypatch):
    """B passes the early check; A registers the same path before B's write: B's transaction refuses."""
    conn, b, tid, source, _scratch, _home, _clone = setup
    other = _second_task(conn, tmp_path, f"wt/{tid}")
    original = reg.source_identity
    raced = []

    def racing_identity(path, kind, **kwargs):
        if not raced:
            raced.append(True)
            reg.register_initial(conn, tid, str(source))  # the competing registration commits first
        return original(path, kind, **kwargs)

    monkeypatch.setattr(reg, "source_identity", racing_identity)
    with pytest.raises(launch.AttachRefused) as exc:
        reg.register_initial(conn, other, str(source))
    assert raced and "another active task" in exc.value.detail
    assert board.attached_run(conn, kb.get_task(conn, other).current_run_id) is None
    assert board.attached_run(conn, kb.get_task(conn, tid).current_run_id).state == "attached"
    attaches = [kw for name, kw in _source_calls(b) if kw.get("mode") == "initial"]
    assert len(attaches) == 1, attaches


def test_a_definitely_refused_observation_owns_nothing(setup, tmp_path):
    conn, b, tid, source, _scratch, _home, _clone = setup
    b.refuse["source_attach"] = "initial_attach_refused"
    with pytest.raises(launch.AttachRefused) as exc:
        reg.register_initial(conn, tid, str(source))
    assert not exc.value.uncertain
    assert board.attached_run(conn, kb.get_task(conn, tid).current_run_id).state == "refused"
    b.refuse.clear()
    other = _second_task(conn, tmp_path, f"wt/{tid}")
    assert reg.register_initial(conn, other, str(source)).state == "attached"


def test_refused_replay_rechecks_ownership_inside_the_observation_transaction(setup, tmp_path, monkeypatch):
    """X is definitely refused; X replays while Y registers the same path first: X's transaction refuses."""
    conn, b, tid, source, _scratch, _home, _clone = setup
    b.refuse["source_attach"] = "initial_attach_refused"
    with pytest.raises(launch.AttachRefused):
        reg.register_initial(conn, tid, str(source))
    b.refuse.clear()
    own = kb.get_task(conn, tid).current_run_id
    assert board.attached_run(conn, own).state == "refused"
    other = _second_task(conn, tmp_path, f"wt/{tid}")
    original = reg.source_identity
    raced = []

    def racing_identity(path, kind, **kwargs):
        if not raced:
            raced.append(True)
            assert reg.register_initial(conn, other, str(source)).state == "attached"
        return original(path, kind, **kwargs)

    monkeypatch.setattr(reg, "source_identity", racing_identity)
    with pytest.raises(launch.AttachRefused) as exc:
        reg.register_initial(conn, tid, str(source))
    assert raced and "another active task" in exc.value.detail
    assert board.attached_run(conn, own).state == "refused", "a refused replay must not reopen past the recheck"
    assert board.attached_run(conn, kb.get_task(conn, other).current_run_id).state == "attached"
    attaches = [kw for name, kw in _source_calls(b) if kw.get("mode") == "initial"]
    assert len(attaches) == 2, "X's refused first attach and Y's attach; X's replay must not reach the Board"


def test_refused_replay_without_a_competitor_reopens_and_attaches(setup):
    conn, b, tid, source, _scratch, _home, _clone = setup
    b.refuse["source_attach"] = "initial_attach_refused"
    with pytest.raises(launch.AttachRefused):
        reg.register_initial(conn, tid, str(source))
    b.refuse.clear()
    assert reg.register_initial(conn, tid, str(source)).state == "attached"


def test_refused_replay_reserves_path_before_attach_rpc(setup, tmp_path, monkeypatch):
    conn, b, tid, source, _scratch, _home, _clone = setup
    b.refuse["source_attach"] = "initial_attach_refused"
    with pytest.raises(launch.AttachRefused):
        reg.register_initial(conn, tid, str(source))
    b.refuse.clear()
    other = _second_task(conn, tmp_path, f"wt/{tid}")
    original = reg._attach_settled
    checked = []

    def competing_attach(connection, run, mode):
        assert board.attached_run(connection, run.run_id).state == "pending"
        with pytest.raises(launch.AttachRefused) as exc:
            reg.register_initial(connection, other, str(source))
        assert "another active task" in exc.value.detail
        checked.append(True)
        return original(connection, run, mode)

    monkeypatch.setattr(reg, "_attach_settled", competing_attach)
    assert reg.register_initial(conn, tid, str(source)).state == "attached"
    assert checked == [True]
    assert board.attached_run(conn, kb.get_task(conn, other).current_run_id) is None


def test_a_finished_task_releases_its_external_worktree(setup, tmp_path):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    reg.register_initial(conn, tid, str(source))
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    conn.commit()
    other = _second_task(conn, tmp_path, f"wt/{tid}")
    assert reg.register_initial(conn, other, str(source)).state == "attached"


# --- P2: premanifest recovery stays inside the materialize budget -----------------------------------------------

def _premanifest(conn, tid, source):
    first = reg.register_initial(conn, tid, str(source))
    dispatch.successor(conn, tid)
    snapshot, _ = dispatch.grant_snapshot(conn, tid, first)
    return first, snapshot


def test_premanifest_recovery_refuses_past_the_budget(setup, monkeypatch):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    first, snapshot = _premanifest(conn, tid, source)
    now = [0.0]
    original = reg.source_identity
    proofs = []

    def expensive_identity(*args, **kwargs):
        proofs.append(kwargs.get("bound"))
        result = original(*args, **kwargs)
        now[0] += 30
        return result

    monkeypatch.setattr(reg, "source_identity", expensive_identity)
    cfg = replace(ws.settings(), materialize_timeout_s=1)
    with pytest.raises(ws.WorkspaceSourceRefusal) as exc:
        resolver.resolve_source_workspace(conn, kb.get_task(conn, tid), snapshot, me=first.machine, cfg=cfg,
                                          clock=lambda: now[0])
    assert exc.value.code == "materialize_timeout"
    assert len(proofs) == 1, "artifact verification must reuse the resolver's recovered predecessor"
    assert proofs[0] is not None, "the identity proof must run under the materialize budget"


def test_premanifest_recovery_inside_the_budget_proves_once(setup, monkeypatch):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    first, snapshot = _premanifest(conn, tid, source)
    original = reg.source_identity
    proofs = []

    def counted(*args, **kwargs):
        proofs.append(kwargs.get("bound"))
        return original(*args, **kwargs)

    monkeypatch.setattr(reg, "source_identity", counted)
    cfg = replace(ws.settings(), materialize_timeout_s=60)
    resolved = resolver.resolve_source_workspace(conn, kb.get_task(conn, tid), snapshot, me=first.machine, cfg=cfg)
    assert resolved.path == str(source) and resolved.mode == "resume"
    assert resolved.predecessor_run_uid == first.run_uid
    assert len(proofs) == 1 and proofs[0] is not None


def test_spent_budget_during_recovery_git_is_a_timeout_not_an_identity_refusal(setup):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    first, snapshot = _premanifest(conn, tid, source)
    now = [0.0]

    def clock():
        now[0] += 5  # every budget read costs 5 s of a 1 s budget
        return now[0]

    cfg = replace(ws.settings(), materialize_timeout_s=1)
    with pytest.raises(ws.WorkspaceSourceRefusal) as exc:
        resolver.resolve_source_workspace(conn, kb.get_task(conn, tid), snapshot, me=first.machine, cfg=cfg,
                                          clock=clock)
    assert exc.value.code == "materialize_timeout"


# --- P3: initial registration honours the host gate -------------------------------------------------------------

def test_disabled_host_refuses_initial_before_persistence_or_rpc(setup, monkeypatch):
    conn, b, tid, source, _scratch, _home, _clone = setup
    cfg = replace(ws.settings(), enabled=False, requested=False)
    monkeypatch.setattr(ws, "settings", lambda: cfg)
    before = len(_source_calls(b))
    with pytest.raises(launch.AttachRefused) as exc:
        reg.register_initial(conn, tid, str(source))
    assert exc.value.code == "source_disabled"
    assert board.attached_run(conn, kb.get_task(conn, tid).current_run_id) is None
    assert len(_source_calls(b)) == before


def test_disabled_host_refuses_heartbeat_registration(setup, monkeypatch):
    from tools import kanban_tools as kt
    conn, b, tid, source, _scratch, _home, _clone = setup
    run_id = kb.get_task(conn, tid).current_run_id
    cfg = replace(ws.settings(), enabled=False, requested=False)
    monkeypatch.setattr(ws, "settings", lambda: cfg)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    @contextlib.contextmanager
    def board_context(*a, **k):
        yield kb, conn

    monkeypatch.setattr(kt, "_board", board_context)
    before = len(_source_calls(b))
    out = json.loads(kt._handle_heartbeat({"task_id": tid, "note": "adopt", "source_path": str(source)}))
    assert "source_disabled" in out.get("error", ""), out
    assert board.attached_run(conn, run_id) is None
    assert len(_source_calls(b)) == before
