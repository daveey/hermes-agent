"""Trusted capsule admission: a host-registered scoped transport may admit a non-local profile to an
exact project worktree, at create and at dispatch, without weakening any other check.

Real core imports against an isolated HERMES_HOME, real profiles, a real git repo and project, a real
board DB with a persisted dispatch owner. The only replaced seam is the host control-plane guard in the
positive fixture (this suite may run under a fenced Kanban worker); the guard itself is exercised
unmocked in ``test_registration_is_host_control_plane_only``.
"""
from __future__ import annotations

import copy
import pickle
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import projects_db as pdb
from hermes_cli.kanban_capabilities import CapabilityMismatchError, validate_assignment
from hermes_cli.kanban_dispatch_owner import set_dispatch_owner

OWNER = "capsule-owner"
DOCKER = "platform_toolsets:\n  cli: [terminal, file]\nterminal:\n  backend: docker\n"
_FENCE_ENV = ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_KANBAN_TASK",
              "HERMES_KANBAN_WORKER_TASK")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path.resolve()


def _profile(home: Path, name: str, config: str) -> Path:
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml

    path = home / "profiles" / name
    path.mkdir(parents=True)
    (path / "config.yaml").write_text(config + _managed_yaml(), encoding="utf-8")
    return path.resolve()


class Env:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / ".hermes"
        self.home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_HOME", str(self.home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(self.home))
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", *_FENCE_ENV):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "normal")
        self.builder = _profile(self.home, "builder", DOCKER)
        self.other = _profile(self.home, "other", DOCKER)
        self.noshell = _profile(self.home, "noshell", "platform_toolsets:\n  cli: [file]\nterminal:\n  backend: docker\n")
        _profile(self.home, "localdev", DOCKER.replace("docker", "local"))
        self.repo = _repo(tmp_path / "repo")
        self.other_repo = _repo(tmp_path / "other-repo")
        with pdb.connect_closing() as pc:
            self.project = pdb.get_project(pc, pdb.create_project(pc, name="Capsule", folders=[str(self.repo)]))
            self.other_project = pdb.get_project(
                pc, pdb.create_project(pc, name="Elsewhere", folders=[str(self.other_repo)]))
        assert self.project.primary_path == str(self.repo)
        self.db = self.home / "kanban.db"
        self.conn = kbc.connect(self.db)
        set_dispatch_owner(self.db, OWNER)
        self.calls: list[tuple[str, str]] = []
        self.routes = {}
        self.spawn = self._record  # one callable object: dispatch binds callable identity

    @property
    def admission(self):
        from hermes_cli import kanban_capsule_admission as admission
        return admission

    def _record(self, task, workspace, board=None):
        self.calls.append((task.id, workspace))
        self.routes[task.id] = (task.model_override, task.provider_override)

    def register(self, **overrides):
        args = dict(board_path=self.db, dispatch_owner=OWNER,
                    profiles={"builder": self.builder, "noshell": self.noshell},
                    project_id=self.project.id, repo_root=self.repo, spawn_fn=self.spawn)
        args.update(overrides)
        return self.admission.register_transport(**args)

    def create(self, conn=None, **kw):
        kw.setdefault("title", "Capsule work")
        kw.setdefault("assignee", "builder")
        kw.setdefault("project_id", self.project.id)
        return kb.create_task(conn or self.conn, **kw)

    def count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """Isolated board/profiles/repo without touching the admission module."""
    e = Env(tmp_path, monkeypatch)
    yield e
    e.conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from hermes_cli import kanban_capsule_admission as admission

    # Fixture-only: this suite may run inside a fenced Kanban worker. The guard is tested unmocked below.
    monkeypatch.setattr(admission, "_require_host_control_plane", lambda path: None)
    e = Env(tmp_path, monkeypatch)
    yield e
    e.conn.close()
    admission._reset_for_tests()


def _missing(err) -> list[str]:
    return list(err.value.missing)


# --- create --------------------------------------------------------------------------------------------

def test_scoped_create_admits_exact_project_worktree(env):
    handle = env.register()
    with handle.create_scope():
        tid = env.create()
    task = kb.get_task(env.conn, tid)
    assert task.assignee == "builder"
    assert task.project_id == env.project.id
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(env.repo / ".worktrees" / tid)
    assert "host_repo" in task.requires  # requirement kept, not removed


def test_docker_worktree_refused_without_registration(env):
    with pytest.raises(CapabilityMismatchError) as err:
        env.create()
    assert _missing(err) == ["host_repo"]
    with pytest.raises(CapabilityMismatchError):
        env.create(project_id=None, workspace_kind="worktree", workspace_path=str(env.tmp / "wt"))
    assert env.count() == 0


def test_registered_but_unscoped_or_revoked_create_refuses(env):
    handle = env.register()
    with pytest.raises(CapabilityMismatchError):
        env.create()
    handle.close()
    with pytest.raises(env.admission.CapsuleAdmissionRefused):
        with handle.create_scope():
            pass
    assert env.count() == 0


@pytest.mark.parametrize("case", ["profile", "project", "explicit_path", "runs_on", "nonproject"])
def test_scoped_create_outside_exact_scope_refuses(env, case):
    handle = env.register()
    kwargs = {
        "profile": dict(assignee="other"),
        "project": dict(project_id=env.other_project.id),
        "explicit_path": dict(workspace_kind="worktree", workspace_path=str(env.repo / ".worktrees" / "x")),
        "runs_on": dict(runs_on=["machine:elsewhere"]),
        "nonproject": dict(project_id="", workspace_kind="worktree", workspace_path=str(env.tmp / "wt")),
    }[case]
    with handle.create_scope(), pytest.raises(CapabilityMismatchError) as err:
        env.create(**kwargs)
    assert "host_repo" in _missing(err)
    assert env.count() == 0


def test_scoped_create_wrong_repo_refuses(env):
    handle = env.register(repo_root=env.other_repo)
    with handle.create_scope(), pytest.raises(CapabilityMismatchError):
        env.create()
    assert env.count() == 0


def test_scoped_create_on_another_board_refuses(env):
    handle = env.register()
    other = kbc.connect(env.tmp / "elsewhere.db")
    try:
        with handle.create_scope(), pytest.raises(CapabilityMismatchError):
            env.create(conn=other)
        assert other.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    finally:
        other.close()


def test_owner_change_revokes_create(env):
    handle = env.register()
    set_dispatch_owner(env.db, "someone-else")
    with handle.create_scope(), pytest.raises(CapabilityMismatchError):
        env.create()
    assert env.count() == 0


def test_other_capabilities_are_still_validated(env):
    handle = env.register()
    with handle.create_scope(), pytest.raises(CapabilityMismatchError) as err:
        env.create(assignee="noshell")
    assert "terminal" in _missing(err) and "host_repo" not in _missing(err)
    assert env.count() == 0


def test_scope_is_thread_local(env):
    handle = env.register()
    seen = {}

    def other_thread():
        conn = kbc.connect(env.db)
        try:
            env.create(conn=conn)
        except CapabilityMismatchError as exc:
            seen["error"] = exc
        finally:
            conn.close()

    with handle.create_scope():
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(30)
        tid = env.create()
    assert isinstance(seen.get("error"), CapabilityMismatchError)
    assert [r[0] for r in env.conn.execute("SELECT id FROM tasks")] == [tid]


def test_no_capability_leakage_outside_create(env):
    handle = env.register()
    with handle.create_scope():
        tid = env.create()
        # Other validation sites never see the scope: plain validator, reassignment, forged attestation.
        with pytest.raises(CapabilityMismatchError):
            validate_assignment("builder", workspace_kind="worktree")
        with pytest.raises(CapabilityMismatchError):
            validate_assignment("builder", workspace_kind="worktree", host_repo_attestation=object())
        with pytest.raises(CapabilityMismatchError):
            kb.assign_task(env.conn, tid, "other")
    assert kb.get_task(env.conn, tid).assignee == "builder"


# --- registration ----------------------------------------------------------------------------------------

def test_handle_is_live_only_and_not_serializable(env):
    handle = env.register()
    for attempt in (lambda: pickle.dumps(handle), lambda: copy.copy(handle), lambda: copy.deepcopy(handle)):
        with pytest.raises(TypeError):
            attempt()
    before = sorted(p.name for p in env.home.iterdir())
    handle.close()
    assert sorted(p.name for p in env.home.iterdir()) == before  # no persisted authority


@pytest.mark.parametrize("case", ["owner", "home", "missing_profile", "noncanonical", "stock_spawn",
                                  "not_callable", "repo_missing", "board_missing", "duplicate"])
def test_registration_refuses_mismatched_binding(env, case):
    if case == "duplicate":
        env.register()
    overrides = {
        "owner": dict(dispatch_owner="someone-else"),
        "home": dict(profiles={"builder": env.other}),
        "missing_profile": dict(profiles={"ghost": env.tmp}),
        "noncanonical": dict(profiles={"Builder": env.builder}),
        "stock_spawn": dict(spawn_fn=kbd._default_spawn),
        "not_callable": dict(spawn_fn=None),
        "repo_missing": dict(repo_root=env.tmp / "nope"),
        "board_missing": dict(board_path=env.tmp / "nope.db"),
        "duplicate": {},
    }[case]
    with pytest.raises(env.admission.CapsuleAdmissionRefused):
        env.register(**overrides)


def test_close_allows_reregistration(env):
    env.register().close()
    env.register().close()


def test_handle_bindings_are_immutable_after_registration(env):
    """[daveey] run27 A-m2: owner, profiles, project, repo root, spawn and board cannot be rebound."""
    handle = env.register()
    other = lambda task, workspace, board=None: None
    attempts = {
        "_spawn": other, "_owner": "someone-else", "_profiles": {"builder": env.other},
        "_project_id": env.other_project.id, "_repo": env.other_repo, "_board": env.tmp / "x.db",
        "_pid": 1, "_closed": False,
        "spawn_fn": other, "dispatch_owner": "someone-else", "profiles": {}, "project_id": "p",
        "repo_root": env.other_repo, "board_path": env.tmp / "x.db",
    }
    for name, value in attempts.items():
        with pytest.raises(AttributeError):
            setattr(handle, name, value)
        with pytest.raises(AttributeError):
            delattr(handle, name)
    with pytest.raises(TypeError):
        handle.profiles["ghost"] = env.tmp  # read-only mapping
    assert handle.spawn_fn is env.spawn and handle.dispatch_owner == OWNER
    assert dict(handle.profiles) == {"builder": env.builder, "noshell": env.noshell}
    assert (handle.project_id, handle.repo_root, handle.board_path) == (env.project.id, env.repo, env.db.resolve())
    assert handle.live
    handle.close()  # close() still revokes
    assert not handle.live
    with pytest.raises(AttributeError):
        handle._closed = False  # a closed handle cannot be revived


def test_registration_is_host_control_plane_only(tmp_path, monkeypatch):
    from agent.delegation_context import delegated_child_context
    from hermes_cli import kanban_capsule_admission as admission

    e = Env(tmp_path, monkeypatch)  # fence env removed: the unmocked guard admits a clean host process
    try:
        e.register().close()
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
        with pytest.raises(admission.CapsuleAdmissionRefused):
            e.register()
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        with delegated_child_context(), pytest.raises(admission.CapsuleAdmissionRefused):
            e.register()
    finally:
        e.conn.close()
        admission._reset_for_tests()


# --- dispatch --------------------------------------------------------------------------------------------

def _admitted(env):
    handle = env.register()
    with handle.create_scope():
        tid = env.create()
    return handle, tid


def _untouched(env, tid, status="ready"):
    task = kb.get_task(env.conn, tid)
    assert task.status == status
    assert task.claim_lock is None and task.current_run_id is None
    assert task.consecutive_failures == 0
    assert env.conn.execute("SELECT count(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
    assert not Path(task.workspace_path).exists()


def _admitted_review(env):
    """Source-linked downstream review keeps the capsule itself unclaimed/unmaterialized."""
    from hermes_cli.kanban_review_model import implementer_models

    source = kb.create_task(env.conn, title="Capsule implementation", assignee="localdev")
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER, max_spawn=1)
    assert [row[0] for row in result.spawned] == [source], result
    source_task = kb.get_task(env.conn, source)
    assert source_task is not None and source_task.current_run_id is not None
    run = source_task.current_run_id
    kb._append_event(env.conn, source, "worker_result",
                     {"route_model": "claude-sonnet-5-5", "route_provider": "anthropic"}, run_id=run)
    assert kb.complete_task(env.conn, source, summary="Fixture implementation complete")
    handle = env.register()
    with handle.create_scope():
        tid = env.create(title="Independent capsule review", creator_task_id=source)
    assert kb.request_review(env.conn, tid)
    assert implementer_models(env.conn, kb.get_task(env.conn, tid)) == {"claude-sonnet-5-5"}
    env.calls.clear()  # implementation is not part of the capsule claim under test
    _untouched(env, tid, status="review")
    return handle, tid


def test_registered_dispatch_spawns_exact_worktree(env):
    _, tid = _admitted(env)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert [t for t, _a, _w in result.spawned] == [tid]
    assert env.calls == [(tid, str(env.repo / ".worktrees" / tid))]
    assert (env.repo / ".worktrees" / tid / ".git").exists()
    assert kb.get_task(env.conn, tid).status == "running"


def test_registered_dry_run_is_admitted(env):
    _, tid = _admitted(env)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER, dry_run=True)
    assert [t for t, _a, _w in result.spawned] == [tid]
    assert result.capsule_refused == []
    _untouched(env, tid)


def test_stock_dispatcher_refuses_even_as_owner(env, monkeypatch):
    _, tid = _admitted(env)
    stock = Mock(side_effect=AssertionError("stock spawn ran a capsule card"))
    monkeypatch.setattr(kbd, "_default_spawn", stock)
    for dry_run in (True, False):
        result = kbd.dispatch_once(env.conn, dispatcher_id=OWNER, dry_run=dry_run)
        assert result.spawned == []
        assert result.capsule_refused == [(tid, "capsule_spawn_mismatch")]
    stock.assert_not_called()
    _untouched(env, tid)


def test_foreign_callback_with_same_owner_refuses(env):
    _, tid = _admitted(env)
    foreign = Mock(return_value=None)
    result = kbd.dispatch_once(env.conn, spawn_fn=foreign, dispatcher_id=OWNER)
    assert result.capsule_refused == [(tid, "capsule_spawn_mismatch")]
    foreign.assert_not_called()
    _untouched(env, tid)


def test_unregistered_process_refuses(env):
    handle, tid = _admitted(env)
    handle.close()  # a restarted process has no registration either
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert result.capsule_refused == [(tid, "capsule_unregistered")]
    assert env.calls == []
    _untouched(env, tid)


def test_owner_change_after_admission_refuses(env):
    _, tid = _admitted(env)
    set_dispatch_owner(env.db, "someone-else")
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id="someone-else")
    assert result.capsule_refused == [(tid, "capsule_owner_mismatch")]
    assert env.calls == []
    _untouched(env, tid)


@pytest.mark.parametrize("column,value,code", [
    ("assignee", "other", "capsule_profile_mismatch"),
    ("project_id", "p_elsewhere", "capsule_project_mismatch"),
    ("workspace_path", "/tmp/elsewhere", "capsule_workspace_mismatch"),
])
def test_drifted_card_refuses(env, column, value, code):
    _, tid = _admitted(env)
    with kb.write_txn(env.conn):  # fixture: a row drifted outside the admitted scope
        env.conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, tid))
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert result.capsule_refused == [(tid, code)]
    assert env.calls == []


def test_review_lane_refuses_foreign_callback(env, monkeypatch):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    _, tid = _admitted(env)
    assert kb.request_review(env.conn, tid) is True  # real transition, no reviewer reassignment
    foreign = Mock(return_value=None)
    result = kbd.dispatch_once(env.conn, spawn_fn=foreign, dispatcher_id=OWNER)
    assert result.capsule_refused == [(tid, "capsule_spawn_mismatch")]
    foreign.assert_not_called()
    _untouched(env, tid, status="review")


def _docker_row_admitted_elsewhere(env):
    """Fixture: a docker-assigned project worktree row that bypassed create (an older core, a restore)."""
    tid = env.create(assignee=None)
    with kb.write_txn(env.conn):
        env.conn.execute("UPDATE tasks SET assignee = 'builder' WHERE id = ?", (tid,))
    return tid


def test_stock_dispatch_refuses_docker_host_repo_row(plain):
    tid = _docker_row_admitted_elsewhere(plain)
    foreign = Mock(return_value=None)
    result = kbd.dispatch_once(plain.conn, spawn_fn=foreign, dispatcher_id=OWNER)
    assert result.spawned == []
    assert result.capsule_refused == [(tid, "capsule_unregistered")]
    foreign.assert_not_called()
    _untouched(plain, tid)


def test_local_profile_dispatch_without_registration(plain):
    tid = plain.create(assignee="localdev")
    foreign = Mock(return_value=None)
    result = kbd.dispatch_once(plain.conn, spawn_fn=foreign, dispatcher_id=OWNER)
    assert [t for t, _a, _w in result.spawned] == [tid]
    foreign.assert_called_once()


def test_local_profile_dispatch_is_unchanged(env):
    env.register()
    tid = env.create(assignee="localdev")
    foreign = Mock(return_value=None)
    result = kbd.dispatch_once(env.conn, spawn_fn=foreign, dispatcher_id=OWNER)
    assert [t for t, _a, _w in result.spawned] == [tid]
    assert result.capsule_refused == []
    foreign.assert_called_once()
