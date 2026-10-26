"""Standalone, conservative assignment preflight against a profile's CLI policy.

This checks configured capabilities, not executable installation or repository existence.
In particular ``git`` means terminal access, not that a git binary was probed. Remote
backends never prove access to a host repository, even when mounts might make it possible.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from collections.abc import Iterable

_CAPABILITIES = ("terminal", "file", "git", "host_repo", "kanban")
_WORKSPACE_REQUIRES = {
    "scratch": (),
    "dir": ("terminal", "file", "host_repo"),
    "worktree": ("terminal", "file", "git", "host_repo"),
}


class CapabilityMismatchError(ValueError):
    """An assignee cannot prove the requested configured capabilities."""

    code = "assignee_capability_mismatch"

    def __init__(self, assignee: str, missing: Iterable[str], *, reason: str = ""):
        self.assignee = assignee
        self.missing = list(missing)
        message = f"Assignee {assignee!r} lacks required capabilities: {', '.join(self.missing)}"
        if reason:
            message += f" ({reason})"
        super().__init__(message)


def _required_capabilities(requires, workspace_kind: str) -> list[str]:
    if workspace_kind not in _WORKSPACE_REQUIRES:
        raise ValueError(f"Unknown workspace kind: {workspace_kind!r}")
    if requires is None:
        requires = []
    if not isinstance(requires, (list, tuple, set, frozenset)):
        raise ValueError("requires must be a collection of capability names")
    for name in requires:
        if not isinstance(name, str) or name not in _CAPABILITIES:
            raise ValueError(f"Unknown required capability: {name!r}")
    required = set(requires) | set(_WORKSPACE_REQUIRES[workspace_kind])
    return [name for name in _CAPABILITIES if name in required]


def _check_policy_shape(config) -> None:
    """Reject malformed policy rather than the interactive loader's permissive fallback."""
    from hermes_cli.toolset_validation import parse_platform_toolsets_value

    if not isinstance(config, dict):
        raise ValueError("profile config must be a mapping")
    for section in ("platform_toolsets", "terminal", "agent"):
        if section in config and not isinstance(config[section], dict):
            raise ValueError(f"{section} must be a mapping")
    platform = config.get("platform_toolsets", {})
    if "cli" in platform and parse_platform_toolsets_value(platform["cli"]) is None:
        raise ValueError("platform_toolsets.cli must be a list")
    terminal = config.get("terminal", {})
    if "backend" in terminal and (not isinstance(terminal["backend"], str) or not terminal["backend"].strip()):
        raise ValueError("terminal.backend must be a nonempty string")


def _profile_capabilities(assignee: str) -> set[str]:
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.profiles import resolve_profile_env
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import build_profile_terminal_scope, reset_terminal_scope, set_terminal_scope
    from utils import load_yaml_file_readonly

    home = Path(resolve_profile_env(assignee))
    config_path = home / "config.yaml"
    # Missing files and malformed roots are otherwise treated as default policy by the
    # shared loader. That is fine for startup, but cannot establish assignment authority.
    _check_policy_shape(load_yaml_file_readonly(config_path))
    with ExitStack() as stack:
        stack.callback(reset_hermes_home_override, set_hermes_home_override(str(home)))
        stack.callback(reset_secret_scope, set_secret_scope(build_profile_secret_scope(home)))
        config = load_user_config_effective(config_path, fail_closed=True)
        _check_policy_shape(config)
        terminal = build_profile_terminal_scope(home)
        # Effective config includes managed overlays and expanded values; the terminal
        # scope helper also preserves a legacy profile-local .env backend when YAML omits it.
        if "backend" in config.get("terminal", {}):
            terminal["TERMINAL_ENV"] = config["terminal"]["backend"]
        stack.callback(reset_terminal_scope, set_terminal_scope(terminal))
        toolsets = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
        available = set(toolsets) & {"terminal", "file", "kanban"}
        if "terminal" in available:
            available.add("git")
        if terminal.get("TERMINAL_ENV", "").strip().lower() == "local":
            available.add("host_repo")
        return available


def validate_assignment(
    assignee: str | None, *, requires=None, workspace_kind: str = "scratch",
    workspace_path=None, skills=None,
) -> list[str]:
    """Return canonical, deduplicated requirements or raise CapabilityMismatchError.

    Explicit requirements augment workspace inference; even ``requires=[]`` cannot
    disable worktree/dir checks. Unconstrained scratch tasks retain legacy behavior
    without reading a profile config. ``workspace_path`` is not probed: locality is
    established solely by backend policy, not a path existing on the dispatch host.

    Skill-derived requirements are not implemented: there is no established skill
    capability metadata contract. ``skills`` is reserved, and names/prose are never
    guessed into requirements. Callers must supply explicit ``requires`` instead.
    """
    required = _required_capabilities(requires, workspace_kind)
    if not required or assignee is None:
        # Unassigned drafts cannot dispatch; assign_task validates before binding.
        return required
    try:
        available = _profile_capabilities(assignee)
    except Exception as exc:
        raise CapabilityMismatchError(assignee, required, reason=f"profile policy unavailable: {exc}") from exc
    missing = [name for name in required if name not in available]
    if missing:
        raise CapabilityMismatchError(assignee, missing)
    return required
