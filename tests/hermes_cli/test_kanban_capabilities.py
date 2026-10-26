"""Assignment policy checks use real isolated profile configs, not a worker or database."""
from pathlib import Path

import pytest

from hermes_cli.kanban_capabilities import CapabilityMismatchError, validate_assignment


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    def create(name, text):
        target = home if name == "default" else home / "profiles" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.yaml").write_text(text)
        return target

    return create


LOCAL = "platform_toolsets:\n  cli: [terminal, file]\nterminal:\n  backend: local\n"


def test_capable_local_worktree_and_dir(profiles):
    profiles("coder", LOCAL)
    assert validate_assignment("coder", workspace_kind="worktree", workspace_path="/host/repo") == [
        "terminal", "file", "git", "host_repo"
    ]
    assert validate_assignment("coder", workspace_kind="dir") == ["terminal", "file", "host_repo"]


def test_mac_ops_without_terminal_is_rejected(profiles):
    profiles("mac-ops", "platform_toolsets:\n  cli: [file, browser]\n")
    with pytest.raises(CapabilityMismatchError) as caught:
        validate_assignment("mac-ops", workspace_kind="worktree")
    error = caught.value
    assert isinstance(error, ValueError)
    assert error.code == "assignee_capability_mismatch"
    assert error.assignee == "mac-ops"
    assert error.missing == ["terminal", "git"]
    assert "mac-ops" in str(error) and "terminal" in str(error)


@pytest.mark.parametrize("backend", ["docker", "ssh", "modal", "unknown"])
def test_remote_backends_cannot_claim_host_repo(profiles, backend):
    profiles("alpha", LOCAL.replace("local", backend))
    with pytest.raises(CapabilityMismatchError) as caught:
        validate_assignment("alpha", workspace_kind="worktree", workspace_path="/host/repo")
    assert caught.value.missing == ["host_repo"]
    assert validate_assignment("alpha", requires=["terminal", "git"]) == ["terminal", "git"]


def test_explicit_empty_cli_is_not_defaulted(profiles):
    profiles("empty", "platform_toolsets:\n  cli: []\n")
    with pytest.raises(CapabilityMismatchError) as caught:
        validate_assignment("empty", requires=["file", "terminal"])
    assert caught.value.missing == ["terminal", "file"]


def test_explicit_requirements_are_deduplicated_and_added_to_inference(profiles):
    profiles("coder", LOCAL)
    assert validate_assignment("coder", requires=["git", "file", "git"]) == ["file", "git"]
    assert validate_assignment("coder", requires=["git"], workspace_kind="dir") == [
        "terminal", "file", "git", "host_repo"
    ]


def test_empty_requirements_keep_legacy_profiles_without_config(profiles):
    assert validate_assignment("legacy", requires=[]) == []
    assert validate_assignment("legacy") == []


@pytest.mark.parametrize("text", [None, "terminal: [", "[]", "platform_toolsets: {cli: nonsense}", "terminal: []"])
def test_missing_or_invalid_policy_fails_closed(profiles, text):
    if text is not None:
        profiles("broken", text)
    with pytest.raises(CapabilityMismatchError) as caught:
        validate_assignment("broken", requires=["terminal"])
    assert caught.value.missing == ["terminal"]


def test_existing_identity_without_config_fails_closed(profiles):
    target = profiles("legacy", LOCAL)
    (target / "config.yaml").unlink()
    (target / "SOUL.md").write_text("Legacy profile")
    with pytest.raises(CapabilityMismatchError):
        validate_assignment("legacy", requires=["terminal"])


def test_unreadable_policy_fails_closed(profiles, monkeypatch):
    from hermes_cli import config_effective

    profiles("coder", LOCAL)
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(config_effective, "load_user_config_effective", denied)
    with pytest.raises(CapabilityMismatchError):
        validate_assignment("coder", requires=["terminal"])


def test_disabled_toolsets_and_composites_use_effective_cli_policy(profiles):
    profiles("coder", "platform_toolsets:\n  cli: [hermes-cli]\nagent:\n  disabled_toolsets: [terminal]\n")
    with pytest.raises(CapabilityMismatchError) as caught:
        validate_assignment("coder", requires=["git"])
    assert caught.value.missing == ["git"]


def test_target_backend_ignores_ambient_profile(profiles, monkeypatch):
    profiles("coder", LOCAL)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert validate_assignment("coder", requires=["host_repo"]) == ["host_repo"]
    target = profiles("sandbox", "platform_toolsets:\n  cli: [terminal, file]\n")
    (target / ".env").write_text("TERMINAL_ENV=ssh\n")
    with pytest.raises(CapabilityMismatchError):
        validate_assignment("sandbox", requires=["host_repo"])


def test_default_profile_and_implicit_cli_defaults(profiles):
    profiles("default", "terminal: {backend: local}\n")
    assert validate_assignment("default", workspace_kind="worktree") == [
        "terminal", "file", "git", "host_repo"
    ]


@pytest.mark.parametrize("requires", [["telepathy"], "terminal", [42]])
def test_invalid_explicit_requirements_are_not_silently_dropped(profiles, requires):
    with pytest.raises(ValueError):
        validate_assignment("legacy", requires=requires)


def test_unknown_workspace_kind_is_not_silently_unconstrained(profiles):
    with pytest.raises(ValueError):
        validate_assignment("legacy", workspace_kind="worktre")


def test_empty_requirements_cannot_disable_workspace_inference(profiles):
    profiles("empty", "platform_toolsets: {cli: []}\n")
    with pytest.raises(CapabilityMismatchError) as caught:
        validate_assignment("empty", requires=[], workspace_kind="dir")
    assert caught.value.missing == ["terminal", "file"]


def test_missing_file_capability_is_independent_of_terminal(profiles):
    profiles("shell", "platform_toolsets: {cli: [terminal]}\n")
    with pytest.raises(CapabilityMismatchError) as caught:
        validate_assignment("shell", workspace_kind="worktree")
    assert caught.value.missing == ["file"]


def test_profile_scopes_are_restored_on_success_and_failure(profiles):
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import get_terminal_scope

    profiles("coder", LOCAL)
    profiles("broken", "terminal: [")
    original_home = get_hermes_home()
    original_terminal = get_terminal_scope()
    validate_assignment("coder", requires=["terminal"])
    with pytest.raises(CapabilityMismatchError):
        validate_assignment("broken", requires=["terminal"])
    assert get_hermes_home() == original_home
    assert get_terminal_scope() is original_terminal


def test_skill_names_are_not_guessed_as_capabilities(profiles):
    # Skill capability metadata has no established convention; do not infer from names/prose.
    assert validate_assignment("legacy", skills=["git", "terminal"]) == []
