"""Shared capsule-workspace test fixtures (registered for tests/tools by ``conftest.py``). [daveey]

``fake_docker`` replaces Docker with a recording fake CLI (``subprocess.run`` + ``_popen_bash``) so tests
assert on the exact ``docker run`` / ``docker exec`` argv the real code builds; ``capsule`` gives fresh
process binding state, worker identity env and a canonical capsule dir. Imports are lazy so registering
them costs the rest of tests/tools nothing.
"""

import os
import subprocess

import pytest

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
    from tools.environments import docker as docker_env
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
    from hermes_constants import get_hermes_home
    from tools import capsule_workspace
    from tools.capsule_workspace import bind_worker_workspace
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
