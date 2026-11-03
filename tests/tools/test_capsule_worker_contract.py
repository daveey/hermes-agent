"""Capsule worker contract: what a capsule-bound Kanban worker's model is told, and where its tools start.

Run6 (Ask Eve, t_01a19f6eb64ca98b): the capsule really was mounted at /workspace, but the worker's
system prompt said ``cd $HERMES_KANBAN_WORKSPACE`` (unset in the sandbox), its first turn named the HOST
worktree path, generic step 5 said ``kanban_complete`` hands off to a review child, and every terminal
command ran in the profile's ``terminal.cwd`` (/root). The model probed host paths, found nothing and
blocked asking for a host backend and push credentials.

These tests drive the REAL ``_get_env_config`` (the v3 capsule tests monkeypatched it, which is why the
/root default was never caught), the real ``terminal_tool`` entry and the real prompt selection. Each
bound assertion has an unbound control that must stay byte-identical to the generic behaviour.
"""

from __future__ import annotations

import json
import os

import pytest

from tools import capsule_workspace

from tests.tools.test_capsule_workspace import (  # pytest fixtures
    IMAGE, RUN, TASK, VALID_CC, capsule, fake_docker,
)


@pytest.fixture
def docker_env_vars(monkeypatch, tmp_path):
    """The live askeve-builder terminal shape as the bridged TERMINAL_* env (profile terminal.cwd=/root)."""
    import tools.terminal_tool as tt
    for key, value in {
        "TERMINAL_ENV": "docker", "TERMINAL_CWD": "/root", "TERMINAL_DOCKER_IMAGE": IMAGE,
        "TERMINAL_DOCKER_EXTRA_ARGS": json.dumps(["--runtime=runsc"]), "TERMINAL_DOCKER_VOLUMES": "[]",
        "TERMINAL_DOCKER_FORWARD_ENV": "[]", "TERMINAL_DOCKER_ENV": "{}",
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "false", "TERMINAL_CONTAINER_PERSISTENT": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(tt, "_ensure_terminal_env_bridged", lambda: None)
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    return tt


# --- cwd -----------------------------------------------------------------------------------------

def test_unbound_docker_default_cwd_stays_the_profile_cwd(capsule, docker_env_vars):
    assert docker_env_vars._get_env_config()["cwd"] == "/root"


def test_bound_docker_default_cwd_is_the_capsule(capsule, docker_env_vars):
    capsule.bind()
    config = docker_env_vars._get_env_config()
    assert (config["cwd"], config["host_cwd"]) == ("/workspace", None)


def test_bound_non_docker_config_is_not_rewritten(capsule, docker_env_vars, monkeypatch):
    # A bound process refuses every non-docker backend at env creation; the default is left alone.
    capsule.bind()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", "/srv/x")
    assert docker_env_vars._get_env_config()["cwd"] == "/srv/x"


def _exec_script(fake):
    """The bash script the docker backend exec'd for the last command (its leading ``cd``)."""
    return " ".join(fake.execs[-1])


HOST_NATIVE = "/home/ubuntu/code/askeve/.worktrees/t_01a19a6b4fcce737"  # run6's recorded/advertised cwd


@pytest.mark.parametrize("recorded", [HOST_NATIVE, "<tmp-native>", None])
@pytest.mark.parametrize("bound,expected", [(True, "/workspace"), (False, "/root")])
def test_real_terminal_tool_command_cwd(capsule, fake_docker, docker_env_vars, monkeypatch, bound, expected,
                                        recorded):
    """The real ``terminal_tool`` entry: with the worker's HOST native worktree as the session cwd record
    (run6's path, and a /tmp path the generic host-path heuristic does not classify), or none at all."""
    from tools import terminal_tool as tt
    if recorded == "<tmp-native>":
        recorded = os.path.dirname(capsule.ws)  # parent of the mounted capsule, under pytest's /tmp
    if bound:
        capsule.bind()
    monkeypatch.setattr(tt, "get_session_cwd", lambda key: recorded)
    out = json.loads(tt.terminal_tool(command="pwd", task_id="sess-cwd"))
    assert out.get("exit_code") == 0, out
    script = _exec_script(fake_docker)
    if bound:
        assert "builtin cd -- /workspace " in script, script
        assert "/root" not in script.split("eval")[0]
        assert not recorded or recorded not in script
    elif recorded is None or recorded == HOST_NATIVE:
        assert "builtin cd -- /root " in script, script  # control: today's generic behaviour, unchanged


def test_bound_terminal_keeps_a_cd_within_the_capsule(capsule, fake_docker, docker_env_vars, monkeypatch):
    from tools import terminal_tool as tt
    capsule.bind()
    monkeypatch.setattr(tt, "get_session_cwd", lambda key: "/workspace/site")
    json.loads(tt.terminal_tool(command="pwd", task_id="sess-cd"))
    assert "builtin cd -- /workspace/site " in _exec_script(fake_docker)


def test_bound_terminal_explicit_workdir_is_honoured(capsule, fake_docker, docker_env_vars):
    from tools import terminal_tool as tt
    capsule.bind()
    json.loads(tt.terminal_tool(command="pwd", task_id="sess-wd", workdir="/tmp"))
    assert "builtin cd -- /tmp " in _exec_script(fake_docker)


def test_file_ops_env_starts_in_capsule(capsule, fake_docker, docker_env_vars):
    from tools.file_tools import _create_terminal_env_for_file_ops
    capsule.bind()
    env = _create_terminal_env_for_file_ops("sess-file", "sess-file")[1]
    assert env.cwd == "/workspace"


# --- prompt --------------------------------------------------------------------------------------

def test_capsule_guidance_replaces_every_host_workspace_and_completion_line():
    from agent.prompt_builder import KANBAN_GUIDANCE, kanban_capsule_guidance
    g = kanban_capsule_guidance()
    assert "## Capsule contract (authoritative runtime context for this run)" in g
    assert "private git checkout at `/workspace`" in g
    for host_line in ("`cd $HERMES_KANBAN_WORKSPACE`", "your workspace is `$HERMES_KANBAN_WORKSPACE`",
                      "the main repo is two levels up", "a credential only the user holds",
                      "When any pre-created review, QA, or release child depends on your task, call `kanban_complete`"):
        assert host_line in KANBAN_GUIDANCE  # the generic text still says it ...
        assert host_line not in g            # ... the capsule text never does
    assert "kanban_request_review(summary=..., metadata=" in g
    assert "Do not call `kanban_complete` on this card" in g
    assert "safe.directory /workspace" in g
    # Tells the worker NOT to ask for access; never asks for it.
    assert "Never fetch, pull, push, open a PR, or ask for a host backend, a token, an SSH key or a mount" in g
    assert g.index("## Capsule contract") < g.index("## Lifecycle")


def test_capsule_guidance_fails_closed_on_generic_drift(monkeypatch):
    import agent.prompt_builder as pb
    monkeypatch.setattr(pb, "KANBAN_GUIDANCE", pb.KANBAN_GUIDANCE.replace("`cd $HERMES_KANBAN_WORKSPACE` before", "x"))
    with pytest.raises(RuntimeError, match="drifted"):
        pb.kanban_capsule_guidance()


def test_agent_init_selects_capsule_guidance_only_when_bound(capsule, monkeypatch):
    """The selection agent_init._load_tools calls, for a bound and an unbound owned Kanban worker."""
    import inspect
    import agent.agent_init as ai
    import agent.agent_init_capsule as aic
    import agent.delegation_context as dc
    from agent.prompt_builder import KANBAN_GUIDANCE, kanban_capsule_guidance
    monkeypatch.setattr(dc, "owned_kanban_task", lambda: True)
    picked = []
    assert "select_kanban_guidance(agent.valid_tool_names)" in inspect.getsource(ai._load_tools)
    real = aic.select_kanban_guidance
    picked.append(real({"kanban_show"}))
    capsule.bind()
    picked.append(real({"kanban_show"}))
    picked.append(real({"terminal"}))  # no kanban_show: never a Kanban protocol, bound or not
    assert picked == [KANBAN_GUIDANCE, kanban_capsule_guidance(), ""]


# --- worker context (first turn) and kanban_show -------------------------------------------------

HEADER = ("# Kanban task t_x: title\n\nAssignee: askeve-builder\nStatus:   running\n"
          "Workspace: worktree @ {ws}\n\n## Body\nWorkspace: worktree @ {ws}\nbody text\n")


def test_worker_context_header_names_the_capsule_only_when_bound(capsule):
    from gateway.session_kanban_capsule import capsule_worker_context
    native = os.path.dirname(capsule.ws)
    context = {"context": HEADER.format(ws=native), "workspace": native}
    assert capsule_worker_context(context) == context["context"]  # unbound: byte-identical
    capsule.bind()
    out = capsule_worker_context(context)
    head, _, body = out.partition("\n## Body\n")
    assert "Workspace: /workspace (task capsule" in head and native not in head
    assert body == f"Workspace: worktree @ {native}\nbody text\n"  # task-supplied body text is never rewritten


def test_kanban_show_own_card_names_execution_workspace_when_bound(capsule, tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        mine = kb.create_task(conn, title="mine", body="b")
        other = kb.create_task(conn, title="other", body="b")
        native = os.path.dirname(capsule.ws)
        conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=? WHERE id IN (?, ?)",
                     (native, mine, other))
        conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", mine)
    show = lambda tid: json.loads(kanban_tools._handle_show({"task_id": tid}))
    before = show(mine)
    assert "execution_workspace" not in before and f"@ {native}" in before["worker_context"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", TASK)
    capsule.bind()
    monkeypatch.setenv("HERMES_KANBAN_TASK", mine)
    mine_bound, other_bound = show(mine), show(other)
    assert mine_bound["execution_workspace"] == "/workspace"
    assert mine_bound["task"]["workspace_path"] == native  # the row is reported as it is
    assert "Workspace: /workspace (task capsule" in mine_bound["worker_context"]
    assert "execution_workspace" not in other_bound and f"@ {native}" in other_bound["worker_context"]
