"""Task-only capsule workspace binding: the host controller binds ONE exact canonical capsule
directory per dedicated worker process, and every terminal / file / execute_code environment that
process creates (or keeps cached) runs only in a runsc Docker sandbox that mounts that directory and
nothing else. Missing, stale, revoked, forked or drifted state fails closed with no fallback to the
ordinary backend. Without a binding every backend is unchanged.

Docker is replaced by a recording fake CLI (``subprocess.run`` + ``_popen_bash``): the tests assert
on the exact ``docker run`` / ``docker exec`` argv the real code builds.
"""

import copy
import os
import pickle
import subprocess
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home
from tools import capsule_workspace
from tools.capsule_workspace import CapsuleWorkspaceError, bind_worker_workspace
from tools.environments import docker as docker_env
from tools.environments.local import LocalEnvironment
from tools.terminal_tool_backends import _create_environment

TASK, RUN = "t_capsule01", "41"
IMAGE = "python:3.11"
VALID_CC = {
    "container_cpu": 0, "container_memory": 0, "container_disk": 0, "container_persistent": False,
    "docker_volumes": [], "docker_mount_cwd_to_workspace": False, "docker_forward_env": [],
    "docker_env": {}, "docker_extra_args": ["--runtime=runsc"], "docker_shared_container_key": "",
    "docker_snap_compat": False, "docker_network": True, "docker_persist_across_processes": True,
    "docker_run_as_host_user": False, "docker_shm_size": "1g", "docker_image_pinned": False,
    "docker_orphan_reaper": False,
}


class FakeDocker:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.execs: list[list[str]] = []
        self.exec_output = ""
        self.exec_rc = 0

    @property
    def runs(self):
        return [c for c in self.calls if c[1:2] == ["run"]]


@pytest.fixture
def fake_docker(monkeypatch):
    fake = FakeDocker()
    real_popen = subprocess.Popen

    def _run(cmd, **_kw):
        cmd = list(cmd)
        fake.calls.append(cmd)
        out = "fake-container-id\n" if cmd[1:2] == ["run"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    def _popen(cmd, stdin_data=None, **_kw):
        fake.execs.append(list(cmd))
        script = f"printf '%s' {fake.exec_output!r}; exit {fake.exec_rc}"
        return real_popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, text=True)

    monkeypatch.setattr(docker_env, "_cgroup_limits_ok", True)
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env, "_image_uses_init_entrypoint", lambda exe, image: False)
    monkeypatch.setattr(docker_env, "_egress_proxy_args_for_docker", lambda: ([], {}, []))
    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(docker_env, "_popen_bash", _popen)
    return fake


@pytest.fixture
def capsule(tmp_path, monkeypatch):
    """Fresh process binding state, worker identity env and a canonical capsule dir."""
    monkeypatch.setattr(capsule_workspace, "_STATE", capsule_workspace._ProcessState())
    monkeypatch.setenv("HERMES_KANBAN_TASK", TASK)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", RUN)
    workspace = tmp_path / "capsule" / "sandbox"
    workspace.mkdir(parents=True)

    class Ctx:
        live = True
        home = os.path.realpath(get_hermes_home())
        ws = os.path.realpath(workspace)

        def bind(self, **overrides):
            kwargs = dict(task_id=TASK, run_id=RUN, profile_home=self.home, workspace=self.ws,
                          is_live=lambda: self.live)
            kwargs.update(overrides)
            return bind_worker_workspace(**kwargs)

    return Ctx()


def _create(env_type="docker", cc=None, **kw):
    return _create_environment(env_type=env_type, image=IMAGE, cwd=kw.pop("cwd", "/root"), timeout=30,
                               container_config=dict(VALID_CC) if cc is None else cc,
                               task_id=kw.pop("task_id", "sess-1"), host_cwd=kw.pop("host_cwd", None), **kw)


def _volume_specs(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


# --- binding API -------------------------------------------------------------------------------

def test_bind_returns_process_local_handle_that_cannot_be_serialized(capsule):
    handle = capsule.bind()
    assert (handle.task_id, handle.run_id, handle.workspace, handle.profile_home) == (
        TASK, RUN, capsule.ws, capsule.home)
    for leak in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            leak(handle)


@pytest.mark.parametrize("override", [{"task_id": "t_other"}, {"run_id": "42"}, {"task_id": ""}])
def test_bind_refuses_identity_that_differs_from_worker_env(capsule, override):
    with pytest.raises(CapsuleWorkspaceError):
        capsule.bind(**override)
    assert capsule_workspace.current_binding() is None  # a refused bind installs nothing


def test_bind_refuses_when_worker_env_missing(capsule, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    with pytest.raises(CapsuleWorkspaceError):
        capsule.bind()


def test_bind_refuses_noncanonical_or_foreign_profile_home(capsule, tmp_path):
    link = tmp_path / "home-link"
    link.symlink_to(capsule.home)
    other = tmp_path / "other-profile"
    other.mkdir()
    for home in (str(link), str(other), "relative/home"):
        with pytest.raises(CapsuleWorkspaceError):
            capsule.bind(profile_home=home)


def test_bind_refuses_noncanonical_workspace(capsule, tmp_path):
    link = tmp_path / "ws-link"
    link.symlink_to(capsule.ws)
    a_file = tmp_path / "file"
    a_file.write_text("x")
    colon = tmp_path / "evil:/root"
    colon.mkdir(parents=True)
    for ws in (str(link), str(a_file), str(tmp_path / "missing"), "capsule/sandbox",
               capsule.ws + "/", str(colon)):
        with pytest.raises(CapsuleWorkspaceError):
            capsule.bind(workspace=ws)


@pytest.mark.parametrize("is_live", [lambda: False, lambda: 1, lambda: 1 / 0, "yes"])
def test_bind_refuses_unless_liveness_is_true(capsule, is_live):
    with pytest.raises(CapsuleWorkspaceError):
        capsule.bind(is_live=is_live)


def test_one_binding_per_process_even_after_close(capsule):
    handle = capsule.bind()
    with pytest.raises(CapsuleWorkspaceError):
        capsule.bind()
    handle.close()
    with pytest.raises(CapsuleWorkspaceError):
        capsule.bind()


def test_env_and_config_alone_grant_nothing(capsule, fake_docker, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", capsule.ws)
    monkeypatch.setenv("TERMINAL_CWD", capsule.ws)
    assert capsule_workspace.current_binding() is None
    env = _create(cc={**VALID_CC, "docker_extra_args": []})
    assert not any(capsule.ws in spec for spec in _volume_specs(fake_docker.runs[0]))
    assert env.host_cwd is None


# --- without a binding: ordinary backends unchanged --------------------------------------------

def test_without_binding_ordinary_backends_are_unchanged(capsule, fake_docker, tmp_path):
    assert isinstance(_create("local", cc={}, cwd=str(tmp_path)), LocalEnvironment)
    extra = tmp_path / "data"
    extra.mkdir()
    env = _create(cc={**VALID_CC, "docker_volumes": [f"{extra}:/data"], "docker_extra_args": [],
                      "container_persistent": False})
    assert f"{extra}:/data" in _volume_specs(fake_docker.runs[0])
    assert "--runtime=runsc" not in fake_docker.runs[0]
    assert env.cwd == "/root"


# --- bound: enforced runsc capsule transport ---------------------------------------------------

def _assert_capsule_run(fake, env, ws):
    assert len(fake.runs) == 1
    argv = fake.runs[0]
    assert _volume_specs(argv) == [f"{ws}:/workspace"]
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert "--runtime=runsc" in argv
    assert "/root:rw,exec,size=1g" in argv  # tmpfs home, not a host sandbox dir
    assert str(get_hermes_home()) not in " ".join(argv)
    assert (env.cwd, env.host_cwd, env.host_cwd_mount) == ("/workspace", ws, "/workspace")
    assert env._persist_across_processes is False


def test_bound_create_environment_mounts_only_the_capsule(capsule, fake_docker):
    capsule.bind()
    env = _create(cwd="/root", host_cwd="/somewhere/else")
    _assert_capsule_run(fake_docker, env, capsule.ws)
    result = env.execute("pwd")
    assert result["returncode"] == 0
    assert fake_docker.execs[-1][:2] == ["/usr/bin/docker", "exec"]


def _terminal_entry(config, task_id):
    from tools.terminal_tool_lifecycle import _create_configured_env
    return _create_configured_env(config, "docker", image=IMAGE, cwd=config["cwd"], timeout=30,
                                  task_id=task_id, host_cwd=None)


def _file_entry(config, task_id):
    from tools.file_tools import _create_terminal_env_for_file_ops
    return _create_terminal_env_for_file_ops(task_id, task_id)[1]


def _execute_code_entry(config, task_id):
    from tools.code_execution_tool import _get_or_create_env
    return _get_or_create_env(task_id)[0]


ENTRIES = {"terminal": _terminal_entry, "file": _file_entry, "execute_code": _execute_code_entry}


@pytest.fixture
def docker_config(monkeypatch):
    import tools.terminal_tool as tt
    config = {**tt._get_env_config(), **VALID_CC, "env_type": "docker", "docker_image": IMAGE,
              "cwd": "/root"}
    monkeypatch.setattr(tt, "_get_env_config", lambda: dict(config))
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    return config


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_each_tool_entry_gets_the_capsule(capsule, fake_docker, docker_config, entry):
    capsule.bind()
    env = ENTRIES[entry](docker_config, f"sess-{entry}")
    _assert_capsule_run(fake_docker, env, capsule.ws)


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_each_tool_entry_refuses_after_revocation(capsule, fake_docker, docker_config, entry):
    capsule.bind()
    capsule.live = False
    with pytest.raises(CapsuleWorkspaceError):
        ENTRIES[entry](docker_config, f"sess-{entry}")
    assert fake_docker.runs == []


DRIFT = {
    "no runsc": {"docker_extra_args": []},
    "runc": {"docker_extra_args": ["--runtime=runc"]},
    "extra flag": {"docker_extra_args": ["--runtime=runsc", "--privileged"]},
    "persistent": {"container_persistent": True},
    "persistent default": {"container_persistent": None},
    "volume": {"docker_volumes": ["/etc:/host-etc"]},
    "docker env": {"docker_env": {"A": "b"}},
    "forward env": {"docker_forward_env": ["GITHUB_TOKEN"]},
    "shared key": {"docker_shared_container_key": "shared"},
    "snap compat": {"docker_snap_compat": True},
    "broad cwd mount": {"docker_mount_cwd_to_workspace": True},
}


@pytest.mark.parametrize("name", sorted(DRIFT))
def test_bound_refuses_config_drift(capsule, fake_docker, name):
    capsule.bind()
    cc = {**VALID_CC, **DRIFT[name]}
    if cc["container_persistent"] is None:
        del cc["container_persistent"]
    with pytest.raises(CapsuleWorkspaceError):
        _create(cc=cc)
    assert fake_docker.runs == []
    # Drift refuses the request, it does not revoke: corrected config works again.
    _create()
    assert len(fake_docker.runs) == 1


@pytest.mark.parametrize("env_type", ["local", "ssh", "modal", "some_plugin"])
def test_bound_refuses_every_non_docker_backend(capsule, fake_docker, env_type, tmp_path):
    capsule.bind()
    with pytest.raises(CapsuleWorkspaceError):
        _create(env_type, cc={}, cwd=str(tmp_path))


def test_bound_direct_docker_construction_without_handle_is_refused(capsule, fake_docker):
    capsule.bind()
    with pytest.raises(CapsuleWorkspaceError):
        docker_env.DockerEnvironment(image=IMAGE, cwd="/root", extra_args=["--runtime=runsc"],
                                     persist_across_processes=False)
    assert fake_docker.runs == []


def test_bound_docker_refuses_forged_or_widened_kwargs(capsule, fake_docker, tmp_path):
    handle = capsule.bind()
    base = dict(image=IMAGE, cwd="/workspace", host_cwd=capsule.ws, auto_mount_cwd=True,
                extra_args=["--runtime=runsc"], persist_across_processes=False, capsule=handle)
    for widen in ({"volumes": ["/etc:/x"]}, {"host_cwd": str(tmp_path)}, {"persistent_filesystem": True},
                  {"forward_env": ["TOKEN"]}, {"cwd": "/root"}, {"persist_across_processes": True}):
        with pytest.raises(CapsuleWorkspaceError):
            docker_env.DockerEnvironment(**{**base, **widen})
    assert fake_docker.runs == []


def test_bound_disables_automatic_credential_skill_and_cache_mounts(capsule, fake_docker, monkeypatch,
                                                                     tmp_path):
    import tools.credential_files as cf
    secret = tmp_path / "token.json"
    secret.write_text("{}")
    monkeypatch.setattr(cf, "get_credential_file_mounts",
                        lambda: [{"host_path": str(secret), "container_path": "/root/.hermes/token.json"}])
    _create(cc={**VALID_CC, "docker_extra_args": []})  # control: ordinary mode mounts it
    assert any(str(secret) in s for s in _volume_specs(fake_docker.runs[0]))
    fake_docker.calls.clear()
    capsule.bind()
    env = _create()
    _assert_capsule_run(fake_docker, env, capsule.ws)


def test_bound_refuses_when_egress_would_add_mounts_or_env(capsule, fake_docker, monkeypatch):
    capsule.bind()
    monkeypatch.setattr(docker_env, "_egress_proxy_args_for_docker",
                        lambda: (["-v", "/host/ca.pem:/etc/ca.pem:ro"], {"HTTPS_PROXY": "http://p"}, []))
    with pytest.raises(CapsuleWorkspaceError):
        _create()
    assert fake_docker.runs == []


def test_bound_forwards_no_passthrough_credentials(capsule, fake_docker, monkeypatch):
    monkeypatch.setattr(docker_env, "resolve_passthrough_env",
                        lambda *a, **k: ({"GH_TOKEN": "secret"}, set()))
    ordinary = _create(cc={**VALID_CC, "docker_extra_args": []})
    ordinary.execute("true")
    assert "GH_TOKEN" in fake_docker.execs[-1]
    capsule.bind()
    with pytest.raises(CapsuleWorkspaceError):  # the pre-bind cached env is dead now too
        ordinary.execute("true")
    env = _create()
    env.execute("true")
    assert all("GH_TOKEN" not in argv for argv in fake_docker.execs[-2:])


# --- cached environment: validated at every execution and recreation ---------------------------

@pytest.fixture
def bound_env(capsule, fake_docker):
    handle = capsule.bind()
    env = _create()
    assert env.execute("true")["returncode"] == 0
    fake_docker.execs.clear()
    return capsule, handle, env, fake_docker


def _assert_dead(env, fake):
    with pytest.raises(CapsuleWorkspaceError):
        env.execute("cat /workspace/x")
    assert fake.execs == []
    with pytest.raises(CapsuleWorkspaceError):
        _create()


def test_revocation_kills_cached_env_and_latches(bound_env):
    ctx, _handle, env, fake = bound_env
    ctx.live = False
    _assert_dead(env, fake)
    ctx.live = True  # a revoked binding never comes back
    _assert_dead(env, fake)


def test_close_leaves_fail_closed_tombstone_without_fallback(bound_env, tmp_path):
    _ctx, handle, env, fake = bound_env
    handle.close()
    _assert_dead(env, fake)
    with pytest.raises(CapsuleWorkspaceError):
        capsule_workspace.current_binding()
    with pytest.raises(CapsuleWorkspaceError):  # no silent drop to the host backend either
        _create("local", cc={}, cwd=str(tmp_path))


@pytest.mark.parametrize("var,value", [("HERMES_KANBAN_TASK", "t_other"), ("HERMES_KANBAN_RUN_ID", "99")])
def test_task_or_run_change_after_bind_kills_cached_env(bound_env, monkeypatch, var, value):
    _ctx, _handle, env, fake = bound_env
    monkeypatch.setenv(var, value)
    _assert_dead(env, fake)


def test_profile_scope_change_kills_cached_env(bound_env, monkeypatch, tmp_path):
    _ctx, _handle, env, fake = bound_env
    other = tmp_path / "other-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    _assert_dead(env, fake)


def test_workspace_replaced_by_new_directory_kills_cached_env(bound_env, tmp_path):
    ctx, _handle, env, fake = bound_env
    os.rename(ctx.ws, str(tmp_path / "moved"))
    os.mkdir(ctx.ws)  # same path, different inode
    _assert_dead(env, fake)


def test_workspace_swapped_for_symlink_kills_cached_env(bound_env, tmp_path):
    ctx, _handle, env, fake = bound_env
    target = tmp_path / "moved"
    os.rename(ctx.ws, str(target))
    os.symlink(str(target), ctx.ws)  # resolves to the same inode, but the path is no longer canonical
    _assert_dead(env, fake)


def test_workspace_removed_kills_cached_env(bound_env):
    ctx, _handle, env, fake = bound_env
    os.rmdir(ctx.ws)
    _assert_dead(env, fake)


def test_bound_env_never_recreates_a_gone_container(bound_env):
    _ctx, _handle, env, fake = bound_env
    fake.exec_output, fake.exec_rc = "Error: No such container: fake-container-id", 1
    result = env.execute("true")
    assert result["returncode"] != 0
    assert len(fake.runs) == 1  # no second docker run


def test_recreation_revalidates_the_binding(bound_env):
    ctx, _handle, env, fake = bound_env
    ctx.live = False
    with pytest.raises(CapsuleWorkspaceError):
        env._recreate_container()
    assert len(fake.runs) == 1


@pytest.mark.platforms("linux", "macos")
def test_forked_child_cannot_use_the_binding(bound_env):
    _ctx, _handle, env, _fake = bound_env
    pid = os.fork()
    if pid == 0:  # child: every use must refuse
        code = 0
        for attempt in (capsule_workspace.current_binding, lambda: env.execute("true")):
            try:
                attempt()
                code = 1
            except CapsuleWorkspaceError:
                pass
            except BaseException:
                code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert env.execute("true")["returncode"] == 0  # the parent's binding is untouched
