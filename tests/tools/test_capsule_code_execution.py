"""execute_code under a task-only capsule binding: the local backend dispatches straight to the host
session kernel (tools/code_kernel.py) without building an environment, so it must check the
process's capsule binding itself. A bound process (active or revoked) never reaches the host kernel;
an unbound process keeps ordinary local execute_code.

The host kernel is replaced by a recording sentinel that never runs code, so a regression is
observed as a sentinel call, not as host execution.
"""

import json
import os

import pytest

from hermes_constants import get_hermes_home
from tools import capsule_workspace
from tools.capsule_workspace import bind_worker_workspace

TASK, RUN = "t_capsule_code01", "7"
CODE = "print('host kernel reached')"


@pytest.fixture
def kernel_calls(monkeypatch):
    """Sentinel host kernel plus the local-backend config execute_code reads."""
    from tools import approval
    from tools import code_kernel
    from tools import process_registry
    import tools.terminal_tool as tt

    calls = []

    def _sentinel_kernel(code, **kwargs):
        calls.append(code)
        return json.dumps({"status": "success", "output": "sentinel: host kernel was called"})

    config = {**tt._get_env_config(), "env_type": "local"}
    monkeypatch.setattr(tt, "_get_env_config", lambda: dict(config))
    monkeypatch.setattr(code_kernel, "execute_in_session_kernel", _sentinel_kernel)
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: False)
    monkeypatch.setattr(approval, "check_execute_code_guard",
                        lambda code, env_type, has_host_access=False: {"approved": True})
    return calls


@pytest.fixture
def capsule(tmp_path, monkeypatch):
    monkeypatch.setattr(capsule_workspace, "_STATE", capsule_workspace._ProcessState())
    monkeypatch.setenv("HERMES_KANBAN_TASK", TASK)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", RUN)
    workspace = tmp_path / "capsule" / "sandbox"
    workspace.mkdir(parents=True)

    class Ctx:
        live = True

        def bind(self):
            return bind_worker_workspace(TASK, RUN, os.path.realpath(get_hermes_home()),
                                         os.path.realpath(workspace), lambda: self.live)

    return Ctx()


def _run():
    from tools.code_execution_tool import execute_code
    return json.loads(execute_code(CODE, task_id="sess-code"))


def test_unbound_local_execute_code_uses_the_host_kernel(capsule, kernel_calls):
    result = _run()
    assert kernel_calls == [CODE]
    assert result["status"] == "success"


def test_bound_local_execute_code_never_reaches_the_host_kernel(capsule, kernel_calls):
    capsule.bind()
    result = _run()
    assert kernel_calls == []
    assert result.get("status") != "success"
    assert "capsule" in json.dumps(result).lower()


def test_revoked_binding_local_execute_code_never_reaches_the_host_kernel(capsule, kernel_calls):
    capsule.bind()
    capsule.live = False
    result = _run()
    assert kernel_calls == []
    assert result.get("status") != "success"
    assert "capsule" in json.dumps(result).lower()
