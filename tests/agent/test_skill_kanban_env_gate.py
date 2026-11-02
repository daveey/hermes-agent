"""``environments: [kanban]`` skills follow the kanban tool gate, not a bare board pin. [daveey]

Every classic CLI / legacy-TUI chat pins ``HERMES_KANBAN_BOARD`` at boot
(``_pin_kanban_board_env``), so the board pin alone must not turn a chat into
a "kanban worker" for skill relevance: those chats never get the kanban tools.
"""
import pytest

_KANBAN_ENV = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
    "HERMES_ORIGIN_SESSION", "HERMES_DELEGATED_CHILD_CONTEXT",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    for name in _KANBAN_ENV:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text("platform_toolsets:\n  cli: [file, terminal]\n")
    for name, extra in (("ordinary-probe", ""), ("review-probe", "environments: [kanban]\n")):
        skill = tmp_path / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Probe\n{extra}---\nBody\n")
    return tmp_path


def _commands():
    from agent.skill_commands import reload_skills
    reload_skills()
    from agent.skill_commands import get_skill_commands
    return set(get_skill_commands())


def _detect():
    from agent.skill_utils import _detect_kanban, _detect_environment
    return _detect_kanban(), _detect_environment("kanban")


def test_bare_board_pin_hides_kanban_skills(home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    assert _detect() == (False, False)
    commands = _commands()
    assert "/ordinary-probe" in commands
    assert "/review-probe" not in commands


def test_dispatcher_owned_worker_shows_kanban_skills(home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    assert _detect() == (True, True)
    assert "/review-probe" in _commands()


def test_non_owned_worker_env_hides_kanban_skills(home, monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    with non_dispatcher_owned_context():
        assert _detect() == (False, False)


def test_ambient_origin_session_shows_kanban_skills(home, monkeypatch):
    monkeypatch.setenv("HERMES_ORIGIN_SESSION", "@session:default@hub/s1")
    assert _detect() == (True, True)


def test_profile_kanban_toolset_shows_kanban_skills(home):
    (home / "config.yaml").write_text("platform_toolsets:\n  cli: [file, terminal, kanban]\n")
    assert _detect() == (True, True)
    assert "/review-probe" in _commands()


def test_chat_boot_pin_lists_no_kanban_skill(home, monkeypatch):
    import os
    from hermes_cli.main_tui_launch import _pin_kanban_board_env
    monkeypatch.setattr(os, "environ", os.environ.copy())  # the pin writes os.environ directly
    _pin_kanban_board_env()
    assert os.environ.get("HERMES_KANBAN_BOARD") == "default"
    commands = _commands()
    assert "/ordinary-probe" in commands
    assert "/review-probe" not in commands
