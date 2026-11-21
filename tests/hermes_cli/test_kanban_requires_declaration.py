"""Agent-created cards cannot omit ``requires`` past the assignment preflight. [daveey]

The preflight (kanban_capabilities.validate_assignment) only checks a profile when something
names a capability. A scratch card whose creator omitted ``requires`` named nothing, so it went
straight to a profile that could not do the work (an alpha-like sandbox profile got hub repo
jobs and stalled). With ``kanban.require_capability_declaration`` on, every agent create
surface (tool, agent/worker CLI, decomposer) refuses such an omission with the typed
``requires_declaration_missing`` error before anything is written. An explicit ``[]`` still makes a
capability-free card, but it cannot switch off needs implied by structured metadata: the
workspace, or a pinned skill's ``metadata.hermes.requires_toolsets``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_capabilities import (
    CapabilityMismatchError, RequiresDeclarationMissing, skill_requirements, validate_assignment,
)

REPO = Path(__file__).resolve().parents[2]
_AGENT_MARKERS = ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_SESSION_SOURCE",
                  "HERMES_RPC_SOCKET", "HERMES_KERNEL_SENTINEL")

DEFAULT_CFG = ("platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n"
               "kanban:\n  require_capability_declaration: {flag}\n")
# alpha-like: full tools, but a sandboxed (docker) backend: no host repo access.
ALPHA_CFG = "platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: docker\n"
# A profile with no shell or file tools at all (a writer/researcher lane).
WRITER_CFG = "platform_toolsets:\n  cli: [web]\nterminal:\n  backend: local\n"
SHELL_SKILL = ("---\nname: shell-skill\ndescription: Needs a shell.\nmetadata:\n  hermes:\n"
               "    requires_toolsets: [terminal]\n---\n\nRun things.\n")
TOOL_SKILL = ("---\nname: tool-skill\ndescription: Needs read_file.\nmetadata:\n  hermes:\n"
              "    requires_tools: [read_file]\n---\n\nRead things.\n")
PLAIN_SKILL = "---\nname: git\ndescription: A skill named like a capability, no metadata.\n---\n\nx\n"


def _write_skill(home: Path, name: str, text: str) -> None:
    d = home / "skills" / "test" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for var in _AGENT_MARKERS:
        monkeypatch.delenv(var, raising=False)
    (home / "config.yaml").write_text(DEFAULT_CFG.format(flag="true"))
    for name, cfg in (("alpha", ALPHA_CFG), ("writer", WRITER_CFG)):
        p = home / "profiles" / name
        p.mkdir(parents=True)
        (p / "config.yaml").write_text(cfg)
        for skill, text in (("shell-skill", SHELL_SKILL), ("tool-skill", TOOL_SKILL), ("git", PLAIN_SKILL)):
            _write_skill(p, skill, text)
    for skill, text in (("shell-skill", SHELL_SKILL), ("tool-skill", TOOL_SKILL), ("git", PLAIN_SKILL)):
        _write_skill(home, skill, text)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _count() -> int:
    with kbc.connect_closing() as conn:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


def _tool_create(**args) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create(args))


# --- shared boundary -----------------------------------------------------------------------------

def test_db_refuses_omitted_requires_on_unbacked_scratch_card(env):
    with kbc.connect_closing() as conn:
        with pytest.raises(RequiresDeclarationMissing) as err:
            kb.create_task(conn, title="Scratch", assignee="alpha", require_declaration=True)
    assert err.value.code == "requires_declaration_missing"
    assert _count() == 0


def test_structured_worktree_job_needs_no_declaration_and_is_enforced(env, tmp_path):
    """A repo/worktree job omitting requires is checked from its workspace: the sandboxed
    alpha-like profile is refused for host_repo, not asked for a declaration."""
    with kbc.connect_closing() as conn:
        with pytest.raises(CapabilityMismatchError) as err:
            kb.create_task(conn, title="Hub repo fix", assignee="alpha", workspace_kind="dir",
                           workspace_path=str(tmp_path), require_declaration=True)
    assert err.value.missing == ["host_repo"]
    assert _count() == 0


def test_explicit_empty_creates_capability_free_card(env):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Write a poem", assignee="writer", requires=[],
                             require_declaration=True)
        assert kb.get_task(conn, tid).requires == []


def test_explicit_empty_cannot_bypass_skill_metadata(env):
    with kbc.connect_closing() as conn:
        with pytest.raises(CapabilityMismatchError) as err:
            kb.create_task(conn, title="Use the skill", assignee="writer", requires=[],
                           skills=["shell-skill"], require_declaration=True)
    assert err.value.missing == ["terminal"]
    assert _count() == 0


def test_explicit_empty_cannot_bypass_repo_workspace(env, tmp_path):
    with kbc.connect_closing() as conn:
        with pytest.raises(CapabilityMismatchError):
            kb.create_task(conn, title="Repo", assignee="alpha", requires=[], workspace_kind="dir",
                           workspace_path=str(tmp_path), require_declaration=True)
    assert _count() == 0


def test_skill_metadata_backs_an_omitted_declaration_and_is_stored(env):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Skill job", assignee="alpha", skills=["shell-skill"],
                             require_declaration=True)
        assert kb.get_task(conn, tid).requires == ["terminal"]


def test_skill_requirements_read_structured_fields_only(env):
    assert skill_requirements(["shell-skill"], "alpha") == ["terminal"]
    assert skill_requirements(["tool-skill"], "alpha") == ["file"]
    # A skill NAMED like a capability, with no metadata, implies nothing.
    assert skill_requirements(["git"], "alpha") == []
    assert skill_requirements(["missing-skill", "plugin:x"], "alpha") == []


def test_reassignment_rechecks_skill_needs(env):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Skill job", assignee="alpha", skills=["shell-skill"])
        with pytest.raises(CapabilityMismatchError):
            kb.assign_task(conn, tid, "writer")
        assert kb.get_task(conn, tid).assignee == "alpha"


def test_reassigning_a_legacy_card_rechecks_its_pinned_skills(env):
    """A card stored before skill needs were merged has requires='[]'. Only the skills=task.skills
    argument on the reassign/review paths can refuse moving it to a profile without a shell."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Legacy skill job", assignee="alpha", skills=["shell-skill"])
        conn.execute("UPDATE tasks SET requires='[]' WHERE id=?", (tid,))
        conn.commit()
        assert kb.get_task(conn, tid).requires == []
        with pytest.raises(CapabilityMismatchError) as err:
            kb.assign_task(conn, tid, "writer")
        assert err.value.missing == ["terminal"]
        assert kb.get_task(conn, tid).assignee == "alpha"
        # No mass migration: the stored row is untouched by the refused move.
        assert kb.get_task(conn, tid).requires == []


def test_check_order_declaration_then_capability_then_model(env):
    """Neutral selectors cannot switch off declaration/capability validation."""
    route = {"model_override": "class:standard", "provider_override": "auto"}
    with kbc.connect_closing() as conn:
        with pytest.raises(RequiresDeclarationMissing):
            kb.create_task(conn, title="1", assignee="writer", require_declaration=True, **route)
        with pytest.raises(CapabilityMismatchError):
            kb.create_task(conn, title="2", assignee="writer", requires=["terminal"],
                           require_declaration=True, **route)
        with pytest.raises(ValueError, match="model-neutral"):
            kb.create_task(conn, title="3", assignee="alpha", requires=["terminal"],
                           require_declaration=True, model_override="gpt-5", provider_override="anthropic")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        tid = kb.create_task(conn, title="4", assignee="alpha", requires=["terminal"],
                             require_declaration=True, **route)
        task = kb.get_task(conn, tid)
        assert (task.model_override, task.provider_override) == ("class:standard", "auto")
        assert task.requires == ["terminal"]


def test_gate_off_keeps_legacy_behaviour(env):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Legacy", assignee="alpha")
        assert kb.get_task(conn, tid).requires == []


def test_board_policy_reaches_other_profiles_workers(env, monkeypatch):
    """Shared config writes only the default profile; an alpha worker (own HERMES_HOME, no key)
    creating children must still be gated, so the board home's config counts."""
    from hermes_cli.kanban_capabilities import declaration_required
    monkeypatch.setenv("HERMES_HOME", str(env / "profiles" / "alpha"))
    assert "require_capability_declaration" not in (env / "profiles" / "alpha" / "config.yaml").read_text()
    assert declaration_required() is True
    (env / "config.yaml").write_text(DEFAULT_CFG.format(flag="false"))
    assert declaration_required() is False


# --- tool --------------------------------------------------------------------------------------

def test_tool_chat_create_omitted_requires_is_typed_and_writes_nothing(env, monkeypatch):
    out = _tool_create(title="Fix the hub repo", assignee="alpha")
    assert out.get("code") == "requires_declaration_missing", out
    assert out.get("created") is False
    assert "requires" in out["error"] and "[]" in out["error"]
    assert _count() == 0
    # The same call with a declaration goes through the real preflight.
    out = _tool_create(title="Fix the hub repo", assignee="alpha", requires=["host_repo"])
    assert out.get("code") == "assignee_capability_mismatch", out
    assert _count() == 0
    out = _tool_create(title="Fix the hub repo", assignee="default", requires=["terminal", "host_repo"])
    assert out.get("ok"), out
    assert _count() == 1


def test_tool_explicit_empty_and_gate_off(env):
    out = _tool_create(title="Pure reasoning", assignee="writer", requires=[])
    assert out.get("ok"), out
    (env / "config.yaml").write_text(DEFAULT_CFG.format(flag="false"))
    out = _tool_create(title="Legacy omitted", assignee="writer")
    assert out.get("ok"), out


def test_worker_created_child_is_gated(env, monkeypatch):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Orchestrator", assignee="default", requires=["kanban"])
        kb.claim_task(conn, parent)
        run_id = kb._current_run_id(conn, parent)
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = _tool_create(title="Child", assignee="alpha", parents=[parent])
    assert out.get("code") == "requires_declaration_missing", out
    assert _count() == 1
    out = _tool_create(title="Child", assignee="alpha", parents=[parent], requires=["terminal"])
    assert out.get("ok"), out
    assert _count() == 2


def test_tool_gate_runs_before_cross_gateway_placement(env, monkeypatch):
    """An omitted requires is exactly what kanban.placement routes away; the remote side receives it
    as an exempt courier create, so the refusal must happen before anything is sent."""
    from hermes_cli import kanban_placement as kp
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: "macbook")
    monkeypatch.setattr(kp, "config", lambda: {"route": {"macbook": "hub"}})
    monkeypatch.setattr(kp, "target_live", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(kp, "create_remote", lambda view, **kw: sent.append(view) or {
        "ok": True, "request_id": "req-1", "queued_only": True})
    out = _tool_create(title="Fix the hub repo", assignee="default")
    assert out.get("code") == "requires_declaration_missing", out
    assert sent == [] and _count() == 0
    out = _tool_create(title="Pure reasoning", assignee="default", requires=[])
    assert out.get("placed", {}).get("gateway") == "hub", out
    assert len(sent) == 1 and sent[0].get("requires") == [] and _count() == 0


def test_courier_envelope_does_not_carry_the_gate(env, monkeypatch):
    """Courier creates are exempt (the sender gated), and the option never enters the stored intent,
    so an intent recorded before this change still matches on retry."""
    intent = {"request_id": "request-create-001",
              "owner": {"gateway": "worker", "profile": "default", "board": "default"},
              "origin": {"gateway": "origin", "profile": "work", "session": "s_old"}}
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Courier", assignee="alpha", origin_intent=intent,
                             require_declaration=True)
        stored = json.loads(conn.execute("SELECT intent_json FROM origin_creation_intents").fetchone()[0])
        assert "require_declaration" not in stored["options"] and stored["options"]["requires"] is None
        # A retry of the same envelope (as an older sender would store it) still resolves to the card.
        assert kb.create_task(conn, title="Courier", assignee="alpha", origin_intent=intent) == tid
        assert kb.get_task(conn, tid).requires == []


# --- CLI ---------------------------------------------------------------------------------------

def _cli(argv: list[str], capsys) -> tuple[int, str]:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    rc = kc._cmd_create(root.parse_args(["kanban", "create", *argv]))
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


def test_cli_agent_create_parity(env, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cli")  # an agent's terminal/execute_code shell
    rc, out = _cli(["Fix the hub repo", "--assignee", "alpha"], capsys)
    assert rc == 2 and "requires_declaration_missing" in out
    assert _count() == 0
    rc, out = _cli(["Poem", "--assignee", "writer", "--no-requires"], capsys)
    assert rc == 0, out
    for both in (["--no-requires", "--requires", "file"], ["--requires", "file", "--no-requires"]):
        with pytest.raises(SystemExit) as stop:
            _cli(["Both", "--assignee", "writer", *both], capsys)
        assert stop.value.code == 2 and "mutually exclusive" in capsys.readouterr().err
    rc, out = _cli(["Shell", "--assignee", "writer", "--requires", "terminal"], capsys)
    assert rc == 2 and "assignee_capability_mismatch" in out
    assert _count() == 1


def test_cli_interactive_human_keeps_legacy(env, capsys):
    rc, out = _cli(["Human draft", "--assignee", "alpha"], capsys)
    assert rc == 0, out
    assert _count() == 1


def test_cli_from_a_worker_shell_is_gated(env, monkeypatch, capsys):
    """The in_worker branch (HERMES_KANBAN_TASK set, no chat session markers)."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Orchestrator", assignee="default", requires=["kanban"])
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    from hermes_cli import kanban as kmod
    assert not kmod._agent_invoked()
    rc, out = _cli(["Child", "--assignee", "alpha"], capsys)
    assert rc == 2 and "requires_declaration_missing" in out, out
    assert _count() == 1
    rc, out = _cli(["Child", "--assignee", "alpha", "--requires", "terminal"], capsys)
    assert rc == 0, out
    assert _count() == 2


# --- decomposer --------------------------------------------------------------------------------

def test_decomposer_children_must_declare(env):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="Parent", assignee="default", triage=True)
        with pytest.raises(RequiresDeclarationMissing) as err:
            decompose_triage_task(conn, root, root_assignee="default", require_declaration=True, children=[
                {"title": "Declared", "assignee": "alpha", "requires": ["terminal"]},
                {"title": "Omitted", "assignee": "alpha"},
            ])
        assert err.value.where == "tasks[1].requires"
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        ids = decompose_triage_task(conn, root, root_assignee="default", require_declaration=True, children=[
            {"title": "Declared", "assignee": "alpha", "requires": ["terminal"]},
            {"title": "Free", "assignee": "writer", "requires": []},
        ])
        assert len(ids) == 2


def test_decompose_task_reasks_once_with_the_error(env, monkeypatch):
    from hermes_cli import kanban_decompose as kd
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="Parent", body="do it", assignee="default", triage=True)
    answers = [
        {"fanout": True, "rationale": "r", "tasks": [{"title": "A", "assignee": "alpha", "body": "x"}]},
        {"fanout": True, "rationale": "r", "tasks": [{"title": "A", "assignee": "alpha", "body": "x",
                                                       "requires": ["terminal"]}]},
    ]
    prompts: list[str] = []

    def fake_aux(*a, user, **kw):
        prompts.append(user)
        return json.dumps(answers[len(prompts) - 1]), ""

    monkeypatch.setattr(kd, "_call_aux", fake_aux)
    outcome = kd.decompose_task(root, author="test")
    assert outcome.ok, outcome
    assert len(prompts) == 2 and "requires_declaration_missing" in prompts[1]
    with kbc.connect_closing() as conn:
        kid = outcome.child_ids[0]
        assert kb.get_task(conn, kid).requires == ["terminal"]


def test_decompose_task_gives_up_after_one_reask(env, monkeypatch):
    from hermes_cli import kanban_decompose as kd
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="Parent", body="do it", assignee="default", triage=True)
    omitted = {"fanout": True, "rationale": "r", "tasks": [{"title": "A", "assignee": "alpha", "body": "x"}]}
    prompts: list[str] = []
    monkeypatch.setattr(kd, "_call_aux", lambda *a, user, **kw: prompts.append(user) or (json.dumps(omitted), ""))
    outcome = kd.decompose_task(root, author="test")
    assert not outcome.ok and outcome.reason.startswith("requires_declaration_missing")
    assert len(prompts) == 2
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert kb.get_task(conn, root).status == "triage"


def test_decompose_task_does_not_reask_other_failures(env, monkeypatch):
    from hermes_cli import kanban_decompose as kd
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="Parent", body="do it", assignee="default", triage=True)
    # A declared-but-unservable child: a capability mismatch is not the decomposer's declaration error.
    bad = {"fanout": True, "rationale": "r", "tasks": [{"title": "A", "assignee": "writer", "body": "x",
                                                        "requires": ["terminal"]}]}
    prompts: list[str] = []
    monkeypatch.setattr(kd, "_call_aux", lambda *a, user, **kw: prompts.append(user) or (json.dumps(bad), ""))
    outcome = kd.decompose_task(root, author="test")
    assert not outcome.ok and len(prompts) == 1, outcome


# --- real CLI process, disposable home ---------------------------------------------------------

def test_real_cli_process_uses_disposable_db_and_refuses(env, tmp_path):
    db = env / "kanban.db"
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("HERMES_") and k not in ("PYTHONPATH",)}
    child_env.update(HERMES_HOME=str(env), HERMES_KANBAN_DB=str(db), HERMES_SESSION_SOURCE="cli",
                     HOME=str(tmp_path), PYTHONPATH=str(REPO))
    probe = subprocess.run(
        [sys.executable, "-c", "import os; from hermes_cli import kanban_db as k; "
         "print(os.environ['HERMES_HOME']); print(k.kanban_db_path())"],
        cwd=REPO, env=child_env, capture_output=True, text=True, timeout=120, check=False)
    assert probe.returncode == 0, probe.stderr
    home_line, db_line = probe.stdout.strip().splitlines()[-2:]
    assert Path(home_line) == env and Path(db_line) == db
    assert str(tmp_path) in db_line  # never the production board
    run = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "create", "Fix the hub repo",
         "--assignee", "alpha"],
        cwd=REPO, env=child_env, capture_output=True, text=True, timeout=180, check=False)
    assert run.returncode == 2, (run.stdout, run.stderr)
    assert "requires_declaration_missing" in run.stdout + run.stderr
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    ok = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "create", "Poem", "--assignee", "writer",
         "--no-requires"],
        cwd=REPO, env=child_env, capture_output=True, text=True, timeout=180, check=False)
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    with kbc.connect_closing() as conn:
        rows = conn.execute("SELECT title, status FROM tasks").fetchall()
    # Created in the disposable board and nothing dispatched it (no worker run).
    assert [r[0] for r in rows] == ["Poem"]
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
