"""``agent.global_context_files`` [daveey]: user-level instruction files (e.g. ``~/AGENTS.md``) loaded for every
session of a profile whatever its cwd — project chats, Kanban workers in scratch dirs, cron jobs without a workdir —
before the project chain, deduplicated against it, and absent (upstream behaviour) when unset."""

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_file_sources import context_file_sources_for_agent, list_context_file_sources
from agent.prompt_builder import build_context_files_prompt, global_context_file_paths
from agent.system_prompt import build_system_prompt_parts

RULES = "GLOBAL RULE: Problems You Find — fix, start or propose."


def _agent(**overrides):
    base = dict(
        load_soul_identity=False, skip_context_files=False, valid_tool_names=[], _task_completion_guidance=False,
        _tool_use_enforcement=False, _environment_probe=False, _kanban_worker_guidance="", _memory_store=None,
        _memory_manager=None, model="", provider="", platform="", pass_session_id=False, session_id="",
        _emit_status=lambda *_a, **_k: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def profile(tmp_path):
    """A profile home whose config sets agent.global_context_files to a user-level rules file."""
    home = tmp_path / "profile"
    home.mkdir()
    rules = tmp_path / "user" / "AGENTS.md"
    rules.parent.mkdir()
    rules.write_text(RULES)

    def configure(paths):
        lines = "".join(f"    - {p}\n" for p in paths)
        (home / "config.yaml").write_text(f"agent:\n  global_context_files:\n{lines}" if paths else "agent: {}\n")

    configure([rules])
    return SimpleNamespace(home=home, rules=rules, configure=configure)


def _context(agent, cwd, home):
    os.environ["TERMINAL_CWD"] = str(cwd)
    with (
        patch("agent.prompt_builder.load_soul_md", return_value="SOUL IDENTITY"),
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.system_prompt._agent_home", return_value=home),
    ):
        return build_system_prompt_parts(agent)


@pytest.fixture(autouse=True)
def _restore_terminal_cwd(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)


def test_project_session_loads_global_file_before_project_chain(profile, tmp_path):
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / "AGENTS.md").write_text("PROJECT RULE: use ruff.")
    parts = _context(_agent(), project, profile.home)
    ctx = parts["context"]
    assert RULES in ctx and "PROJECT RULE: use ruff." in ctx
    assert ctx.index(RULES) < ctx.index("PROJECT RULE")  # right after SOUL.md (stable tier), before the project
    assert "## ~/" in ctx or f"## {profile.rules} (global)" in ctx  # provenance-labelled section
    assert "SOUL IDENTITY" in parts["stable"]


def test_kanban_worker_in_scratch_dir_loads_global_file(profile, tmp_path):
    scratch = tmp_path / "kanban" / "workspaces" / "t_1"
    scratch.mkdir(parents=True)
    ctx = _context(_agent(platform="kanban"), scratch, profile.home)["context"]
    assert RULES in ctx and ctx.count(RULES) == 1


def test_cron_without_workdir_loads_global_file_only(profile, tmp_path):
    """Cron: skip_context_files=True + load_soul_identity=True keeps SOUL.md and gets the global rules too."""
    project = tmp_path / "repo"
    project.mkdir()
    (project / "AGENTS.md").write_text("PROJECT RULE: use ruff.")
    ctx = _context(_agent(platform="cron", skip_context_files=True, load_soul_identity=True), project,
                   profile.home)["context"]
    assert RULES in ctx and "PROJECT RULE" not in ctx
    # A plain skip_context_files session (subagents, batch) stays bare.
    bare = _context(_agent(skip_context_files=True), project, profile.home)["context"]
    assert RULES not in bare


def test_cwd_equal_to_home_loads_the_file_once(profile):
    """cwd == the directory holding the global file (Desktop chat in ~): the chain already loads it."""
    ctx = _context(_agent(), profile.rules.parent, profile.home)["context"]
    assert ctx.count(RULES) == 1
    # A symlinked copy elsewhere (hub: ~/AGENTS.md -> sparse checkout) dedupes by content too.
    link = profile.rules.parent / "linked"
    link.mkdir()
    (link / "AGENTS.md").symlink_to(profile.rules)
    assert _context(_agent(), link, profile.home)["context"].count(RULES) == 1


def test_unset_default_is_upstream_behaviour(profile, tmp_path):
    profile.configure([])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert global_context_file_paths(profile.home) == []
    ctx = _context(_agent(), scratch, profile.home)["context"]
    assert RULES not in ctx
    project = tmp_path / "repo"
    project.mkdir()
    (project / "AGENTS.md").write_text("PROJECT RULE")
    assert build_context_files_prompt(cwd=str(project), skip_soul=True, home_override=profile.home) == (
        "# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n"
        "## AGENTS.md\n\nPROJECT RULE")


def test_unreadable_and_missing_paths_are_skipped_with_a_log_line(profile, tmp_path, caplog):
    locked = tmp_path / "locked.md"
    locked.write_text("SECRET LOCKED RULE")
    locked.chmod(0)
    missing = tmp_path / "nope" / "AGENTS.md"
    profile.configure([missing, locked, profile.rules])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    try:
        with caplog.at_level(logging.INFO, logger="agent.prompt_builder"):
            ctx = _context(_agent(), scratch, profile.home)["context"]
        readable = os.access(locked, os.R_OK)  # root ignores mode bits
    finally:
        locked.chmod(0o600)
    assert RULES in ctx
    assert "SECRET LOCKED RULE" not in ctx or readable
    log = caplog.text
    assert f"global context file {missing} skipped: not found" in log
    if not readable:
        assert f"global context file {locked} skipped: not readable" in log


def test_global_file_honours_the_context_file_cap(profile, tmp_path):
    profile.rules.write_text("x" * 50_000 + " TAIL")
    with patch("agent.prompt_builder._get_context_file_max_chars", return_value=2000):
        out = build_context_files_prompt(cwd=str(tmp_path), skip_soul=True, home_override=profile.home)
    assert len(out) < 4000 and " TAIL" in out  # truncated head+tail, not the full 50k


def test_ambient_profile_config_is_read(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    rules = tmp_path / "rules.md"
    rules.write_text(RULES)
    cfg = get_hermes_home() / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    old = cfg.read_text() if cfg.exists() else None
    cfg.write_text(f"agent:\n  global_context_files:\n    - {rules}\n")
    try:
        assert global_context_file_paths() == [rules]
        assert RULES in build_context_files_prompt(cwd=str(tmp_path), skip_soul=True)
    finally:
        cfg.write_text(old) if old is not None else cfg.unlink()


def test_context_manifest_lists_global_rows(profile, tmp_path):
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / "AGENTS.md").write_text("PROJECT RULE")
    missing = tmp_path / "gone.md"
    profile.configure([profile.rules, missing])
    rows = list_context_file_sources(cwd=str(project), home_override=profile.home, skip_soul=True)
    by_path = {r["path"]: r for r in rows}
    assert rows[0]["path"] == str(profile.rules) and rows[0]["label"].endswith("(global)")
    assert by_path[str(profile.rules)]["status"] == "loaded"
    assert by_path[str(missing)]["status"] == "missing"
    assert by_path[str(project / "AGENTS.md")]["status"] == "loaded"
    # cwd == the rules' dir: the chain copy loads, the global row reports duplicate (parity with the prompt).
    rows = list_context_file_sources(cwd=str(profile.rules.parent), home_override=profile.home, skip_soul=True)
    statuses = [(r["label"], r["status"]) for r in rows if r["path"] == str(profile.rules)]
    assert ("AGENTS.md", "loaded") in statuses and any(s == "duplicate" for _l, s in statuses)
    # Cron agent (identity slot only): global rows, no project rows.
    cron = _agent(skip_context_files=True, load_soul_identity=True)
    with patch("agent.system_prompt._agent_home", return_value=profile.home):
        rows = context_file_sources_for_agent(cron)
    assert [r["path"] for r in rows] == [str(profile.rules), str(missing)]
