"""A deleted process cwd must not crash system-prompt builds or the compaction rebuild.

A Kanban worker whose workspace dir was removed and recreated mid-run keeps a process cwd
pointing at a deleted inode; ``os.getcwd()`` then raises FileNotFoundError on every call. The
compaction boundary rebuilds the system prompt, context-file discovery fell back to
``os.getcwd()`` and the whole worker died. Discovery now falls back to the configured terminal
cwd, then the Kanban workspace, then $HOME, with one warning.
"""

import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.platforms("linux", "macos")  # Windows cannot delete a process cwd


@pytest.fixture
def deleted_cwd(tmp_path, monkeypatch):
    """chdir into a directory, then delete it; restore the original cwd afterwards."""
    from agent import runtime_cwd
    monkeypatch.setattr(runtime_cwd, "_deleted_cwd_warned", False, raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    original = os.getcwd()
    gone = tmp_path / "removed-workspace"
    gone.mkdir()
    os.chdir(gone)
    gone.rmdir()
    with pytest.raises(FileNotFoundError):
        os.getcwd()
    try:
        yield gone
    finally:
        os.chdir(original)


def _project(tmp_path, name, marker):
    d = tmp_path / name
    d.mkdir()
    (d / "AGENTS.md").write_text(f"# rules\n{marker}\n")
    return d


def test_build_context_files_prompt_falls_back_to_kanban_workspace(deleted_cwd, tmp_path, monkeypatch, caplog):
    from agent.prompt_builder import build_context_files_prompt

    ws = _project(tmp_path, "ws", "KANBAN-WORKSPACE-RULES")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    with caplog.at_level(logging.WARNING):
        out = build_context_files_prompt(skip_soul=True)
        build_context_files_prompt(skip_soul=True)
    assert "KANBAN-WORKSPACE-RULES" in out
    warnings = [r for r in caplog.records if "working directory was deleted" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


def test_fallback_prefers_terminal_cwd_then_workspace_then_home(deleted_cwd, tmp_path, monkeypatch):
    from agent.runtime_cwd import process_cwd_or_fallback

    term = _project(tmp_path, "term", "T")
    ws = _project(tmp_path, "ws", "W")
    home = _project(tmp_path, "home", "H")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    monkeypatch.setenv("TERMINAL_CWD", str(term))
    assert process_cwd_or_fallback() == str(term)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "also-gone"))
    assert process_cwd_or_fallback() == str(ws)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "gone-too"))
    assert process_cwd_or_fallback() == str(home)


def test_resolve_agent_cwd_survives_deleted_process_cwd(deleted_cwd, tmp_path, monkeypatch):
    from agent.runtime_cwd import resolve_agent_cwd

    ws = _project(tmp_path, "ws", "W")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    assert resolve_agent_cwd() == ws


def test_list_context_file_sources_survives_deleted_process_cwd(deleted_cwd, tmp_path, monkeypatch):
    from agent.context_file_sources import list_context_file_sources

    ws = _project(tmp_path, "ws", "W")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    sources = list_context_file_sources(skip_soul=True)
    assert any(s["path"] and str(ws) in str(s["path"]) and s["loaded"] for s in sources), sources


def test_compaction_rebuild_boundary_survives_deleted_process_cwd(deleted_cwd, tmp_path, monkeypatch):
    """The production crash path: compress_context -> _rebuild_system_prompt_at_boundary ->
    build_system_prompt -> _context_files_part -> build_context_files_prompt(cwd=None)."""
    from agent.conversation_compression import _rebuild_system_prompt_at_boundary
    from agent.system_prompt import build_system_prompt, invalidate_system_prompt

    ws = _project(tmp_path, "ws", "KANBAN-WORKSPACE-RULES")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    agent = SimpleNamespace(
        _cached_system_prompt="OLD PROMPT", _cached_system_prompt_static="OLD",
        _memory_store=None, _memory_manager=None, provider="", model="gpt-4o", platform="kanban",
        _memory_enabled=False, _user_profile_enabled=False, load_soul_identity=False,
        skip_context_files=False, valid_tool_names={"terminal"}, _task_completion_guidance=False,
        _parallel_tool_call_guidance=False, _tool_use_enforcement=False, _execution_guidance=False,
        _environment_probe=False, _bot_mode_protocol=False, _kanban_worker_guidance="",
        pass_session_id=False, session_id="s1", _emit_status=lambda *a, **k: None,
    )
    agent._invalidate_system_prompt = lambda: invalidate_system_prompt(agent)
    agent._build_system_prompt = lambda system_message=None: build_system_prompt(agent, system_message)
    with patch("agent.prompt_builder.load_soul_md", return_value=""), \
         patch("agent.prompt_builder.build_environment_hints", return_value="ENV HINTS"), \
         patch("agent.conversation_compression_tools.refresh_agent_tool_definitions", lambda _a: None):
        rebuilt = _rebuild_system_prompt_at_boundary(agent, "")
    assert "KANBAN-WORKSPACE-RULES" in rebuilt
    assert agent._cached_system_prompt == rebuilt


def test_goal_gate_workspace_never_falls_back_to_another_tree(deleted_cwd, tmp_path, monkeypatch):
    """Goal gates must run in the project or nowhere: a deleted launch dir with no configured cwd
    yields (None, None) as before, never $HOME or the Kanban workspace (which could pass a failing goal)."""
    from hermes_cli.goals import _gate_workspace

    ws = _project(tmp_path, "ws", "W")
    home = _project(tmp_path, "home", "H")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    assert _gate_workspace() == (None, None)
    term = _project(tmp_path, "term", "T")
    monkeypatch.setenv("TERMINAL_CWD", str(term))
    assert _gate_workspace() == (str(term), None)


def test_init_never_retargets_home_for_deleted_launch_dir(deleted_cwd, tmp_path, monkeypatch):
    from hermes_cli import init_command

    home = _project(tmp_path, "home", "H")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd", lambda _key: None, raising=False)
    with pytest.raises(FileNotFoundError):
        init_command._resolve_session_cwd("")
