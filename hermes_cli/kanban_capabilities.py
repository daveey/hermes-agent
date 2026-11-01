"""Standalone, conservative assignment preflight against a profile's CLI policy.

This checks configured capabilities, not executable installation or repository existence.
In particular ``git`` means terminal access, not that a git binary was probed. Remote
backends never prove access to a host repository, even when mounts might make it possible.
"""
from __future__ import annotations

from contextlib import ExitStack
import sys
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


class RequiresDeclarationMissing(ValueError):
    """An agent-created card omitted ``requires`` and nothing structured implies its needs. [daveey]

    Agent-correctable: the caller re-issues the same create with an explicit list. Nothing was
    written, and this is never a human decision.
    """

    code = "requires_declaration_missing"

    @property
    def tool_error_fields(self) -> dict:
        """Extra ``tool_error`` fields: agent-correctable, nothing created."""
        return {"rejection": "invalid_args", "created": False, "field": self.where}

    def __init__(self, *, where: str = "requires"):
        self.where = where
        super().__init__(
            f"{where} was omitted, and nothing structured (a dir/worktree workspace, a project, or a "
            "pinned skill's metadata.hermes.requires_toolsets) implies what the worker needs. No card "
            "was created. Re-issue the same create with an explicit list of the configured capabilities "
            "the work needs: terminal (shell commands), file (reading/editing files), git (git "
            "operations), host_repo (a checkout on this dispatcher host), kanban (board tools). Use "
            "[] only when the work needs none of them (e.g. pure reasoning or writing). Namespaced "
            "gateway needs such as ssh:<host> may be added too."
        )


# Toolsets of the capability model, as named in ``toolsets.TOOLSETS`` and in skill frontmatter.
_SKILL_TOOLSET_CAPABILITIES = ("terminal", "file", "kanban")


def declaration_required() -> bool:
    """``kanban.require_capability_declaration`` (default False = upstream). Never raises.

    A board-wide policy: on when the active profile's config OR the board home's config
    (``kanban_home()/config.yaml``, the default profile shared config writes) sets it, so
    workers of other profiles creating children are gated like the default profile. [daveey]
    """
    def _flag(cfg) -> bool:
        return bool(((cfg or {}).get("kanban") or {}).get("require_capability_declaration"))

    try:
        from hermes_cli.config import load_config_readonly
        if _flag(load_config_readonly()):
            return True
    except Exception:  # health: allow BLE001 S110 -- a policy flag read never breaks create; default is off
        pass
    try:
        from hermes_cli.config_backend import config_exists, read_config_doc_readonly
        from hermes_cli.kanban_db import kanban_home
        path = kanban_home() / "config.yaml"
        if config_exists(path):
            return _flag(read_config_doc_readonly(path))
    except Exception:  # health: allow BLE001 S110 -- an unreadable board config means the default (off)
        pass
    return False


def queue_assignees() -> frozenset[str]:
    """Queue-only assignee names declared in ``kanban.queue_assignees``. Never raises. [daveey]

    A queue assignee is a parking name with no Hermes profile on purpose (bb-queue): the
    dispatcher can never spawn it, and an external gate reassigns the card to a real worker
    profile, which is validated then. Read from the active profile's config and the board
    home's config, like :func:`declaration_required`.
    """
    names: set[str] = set()

    def _add(cfg) -> None:
        raw = ((cfg or {}).get("kanban") or {}).get("queue_assignees") or []
        if isinstance(raw, str):
            raw = [raw]
        names.update(n.strip() for n in raw if isinstance(n, str) and n.strip())

    try:
        from hermes_cli.config import load_config_readonly
        _add(load_config_readonly())
    except Exception:  # health: allow BLE001 S110 -- a policy read never breaks create; default is none
        pass
    try:
        from hermes_cli.config_backend import config_exists, read_config_doc_readonly
        from hermes_cli.kanban_db import kanban_home
        path = kanban_home() / "config.yaml"
        if config_exists(path):
            _add(read_config_doc_readonly(path))
    except Exception:  # health: allow BLE001 S110 -- an unreadable board config declares none
        pass
    return frozenset(names)


def _skill_frontmatter(name: str) -> dict | None:
    """Frontmatter of skill *name* resolved the way ``skill_view`` resolves it in this home, or None."""
    from agent.skill_utils import normalize_skill_lookup_name
    from tools.skills_tool import _read_skill_text, _safe_frontmatter, locate_skill, skill_search_dirs

    lookup = normalize_skill_lookup_name(name)
    roots, _active = skill_search_dirs()
    error, _skill_dir, skill_md = locate_skill(lookup, None, roots)
    if error is not None or skill_md is None:
        return None
    frontmatter = _safe_frontmatter(content=_read_skill_text(skill_md))
    return frontmatter if isinstance(frontmatter, dict) else None


def skill_requirements(skills, assignee: str | None) -> list[str]:
    """Capabilities pinned *skills* declare through their structured activation contract. [daveey]

    Reads ``metadata.hermes.requires_toolsets`` / ``requires_tools`` (the fields that already gate
    a skill's visibility, :func:`agent.skill_utils.extract_skill_conditions`) of each skill as the
    assignee's worker would resolve it. Only toolsets of the capability model count (terminal,
    file, kanban; a tool counts through the toolset that owns it in ``toolsets.TOOLSETS``).
    Skill names and prose are never read as requirements. Plugin-qualified skills resolve in the
    worker's own plugin registry and are skipped, as are skills this home cannot resolve (that
    is :func:`hermes_cli.kanban_skill_check.check_task_skills`' refusal). Never raises.
    """
    names = [s for s in (skills or ()) if isinstance(s, str) and s.strip() and ":" not in s]
    if not names:
        return []
    try:
        from agent.skill_utils import extract_skill_conditions
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.kanban_skill_check import _assignee_home
        from toolsets import TOOLSETS

        if not assignee:
            from hermes_cli.kanban_db import _default_ready_assignee
            assignee = _default_ready_assignee()
        home = _assignee_home(assignee)
        if home is None:
            return []
        owner = {tool: ts for ts in _SKILL_TOOLSET_CAPABILITIES
                 for tool in (TOOLSETS.get(ts) or {}).get("tools", ())}
        found: set[str] = set()
        token = set_hermes_home_override(home)
        try:
            for name in names:
                try:
                    frontmatter = _skill_frontmatter(name)
                except Exception:  # health: allow BLE001 -- an unparseable skill adds no needs
                    frontmatter = None
                if not frontmatter:
                    continue
                cond = extract_skill_conditions(frontmatter)
                toolsets = cond.get("requires_toolsets") or []
                tools = cond.get("requires_tools") or []
                for ts in (toolsets if isinstance(toolsets, list) else [toolsets]):
                    if ts in _SKILL_TOOLSET_CAPABILITIES:
                        found.add(ts)
                for tool in (tools if isinstance(tools, list) else [tools]):
                    if isinstance(tool, str) and tool in owner:
                        found.add(owner[tool])
        finally:
            reset_hermes_home_override(token)
        return [c for c in _CAPABILITIES if c in found]
    except Exception:  # health: allow BLE001 -- documented never-raises: skill needs only augment
        return []


def check_declaration(requires, workspace_kind: str, *, skills=None, assignee=None,
                      where: str = "requires") -> None:
    """Raise :class:`RequiresDeclarationMissing` when *requires* is omitted (None) and neither
    the workspace nor pinned skills imply a profile capability. An explicit ``[]`` passes."""
    if requires is not None:
        return
    if any(n in _CAPABILITIES for n in _required_capabilities(None, workspace_kind)):
        return
    if skill_requirements(skills, assignee):
        return
    raise RequiresDeclarationMissing(where=where)


def check_tool_create(requires, *, workspace_kind, project_id, skills, assignee) -> None:
    """kanban_create's gate, run BEFORE cross-gateway placement: an omitted ``requires`` is what
    routes a card to another gateway, where it lands as an exempt courier create. [daveey]"""
    if project_id or (workspace_kind or "scratch") != "scratch" or not declaration_required():
        return
    check_declaration(requires, "scratch", skills=skills, assignee=assignee)


def _required_capabilities(requires, workspace_kind: str) -> list[str]:
    if workspace_kind not in _WORKSPACE_REQUIRES:
        raise ValueError(f"Unknown workspace kind: {workspace_kind!r}")
    if requires is None:
        requires = []
    if not isinstance(requires, (list, tuple, set, frozenset)):
        raise ValueError("requires must be a collection of capability names")
    # Namespaced gateway capabilities (``aws:<acct>:<role>``, ``ssh:<host>`` ...) are needs of
    # the card, not of the profile: kept in order after the profile ones and never checked
    # against profile policy. Placement/routing lives in kanban_capability_routing. [daveey]
    from hermes_cli.kanban_capability_registry import is_gateway_need
    gateway_needs: list[str] = []
    for name in requires:
        if is_gateway_need(name):
            if name not in gateway_needs:
                gateway_needs.append(name)
            continue
        if not isinstance(name, str) or name not in _CAPABILITIES:
            raise ValueError(f"Unknown required capability: {name!r}")
    required = set(requires) | set(_WORKSPACE_REQUIRES[workspace_kind])
    return [name for name in _CAPABILITIES if name in required] + gateway_needs


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


def _cli_toolsets(config: dict, home: Path, needed: set[str] | None) -> set[str]:
    """The profile's cli toolsets, as ``_get_platform_tools`` resolves them after plugin discovery.

    [daveey] With ``needed`` (the toolsets the caller must find), a recorded discovery for this
    profile (``plugins_snapshot.load``: inputs unchanged, no plugin touched a built-in tool or
    toolset) is served instead of importing every plugin; under those conditions discovery cannot
    change whether ``terminal``/``file`` resolve. The served answer is used only when it grants
    every needed toolset; otherwise, and with no usable record, the full discovery runs as before
    and records its result for the next cold process.
    """
    from hermes_cli import plugins as plugins_mod, plugins_snapshot
    from hermes_cli.tools_config import _get_platform_tools

    # agent.disabled_toolsets resolution imports model_tools, whose import is the one-time plugin
    # discovery of a process; importing it with discovery suppressed would leave a long-lived
    # process without plugin tools, so such a first import goes the full way.
    disabled = (config.get("agent") or {}).get("disabled_toolsets")
    if needed is not None and not (disabled and "model_tools" not in sys.modules):
        snapshot = plugins_snapshot.load(home)
        if snapshot is not None:
            with plugins_snapshot.serving(snapshot):
                served = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
            if needed <= served:
                return served
    manager = plugins_mod.get_plugin_manager()
    fresh = not manager._discovered
    stamp = plugins_snapshot.fingerprint(home) if fresh else None
    toolsets = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    if fresh and manager._discovered:
        plugins_snapshot.record(stamp, home)
    return toolsets


def _profile_capabilities(assignee: str, required: list[str] | None = None) -> set[str]:
    """Capabilities ``assignee``'s policy grants. ``required`` (validate_assignment's list) only
    lets the toolset lookup skip plugin discovery when a recorded one already grants it."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.config_backend import read_config_doc_readonly
    from hermes_cli.config_effective import load_profile_config_effective
    from hermes_cli.profiles import resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import build_profile_terminal_scope, reset_terminal_scope, set_terminal_scope

    home = Path(resolve_profile_env(assignee))
    config_path = home / "config.yaml"
    # Missing files and malformed roots are otherwise treated as default policy by the
    # shared loader. That is fine for startup, but cannot establish assignment authority.
    _check_policy_shape(read_config_doc_readonly(config_path))
    with ExitStack() as stack:
        stack.callback(reset_hermes_home_override, set_hermes_home_override(str(home)))
        stack.callback(reset_secret_scope, set_secret_scope(build_profile_secret_scope(home)))
        config = load_profile_config_effective(config_path, fail_closed=True)
        _check_policy_shape(config)
        terminal = build_profile_terminal_scope(home)
        # Effective config includes managed overlays and expanded values; the terminal
        # scope helper also preserves a legacy profile-local .env backend when YAML omits it.
        if "backend" in config.get("terminal", {}):
            terminal["TERMINAL_ENV"] = config["terminal"]["backend"]
        stack.callback(reset_terminal_scope, set_terminal_scope(terminal))
        needed = None
        if required is not None:
            needed = {"terminal"} if {"terminal", "git"} & set(required) else set()
            needed |= {"file"} & set(required)
        toolsets = _cli_toolsets(config, home, needed)
        available = set(toolsets) & {"terminal", "file", "kanban"}
        # [daveey] Every assignee runs as a dispatcher-spawned worker, and model_tools._select_tool_names adds
        # the kanban toolset to every such worker whatever platform_toolsets.cli says; only
        # agent.disabled_toolsets (subtracted last) can take it away. Reading `kanban` from the chat toolsets
        # refused a reviewer's verdict on worker-1 while that reviewer was calling kanban tools
        # (t_01a184ac93d31979).
        disabled = (config.get("agent") or {}).get("disabled_toolsets") or []
        if isinstance(disabled, str):
            disabled = [disabled]
        if "kanban" not in disabled:
            available.add("kanban")
        if "terminal" in available:
            available.add("git")
        if terminal.get("TERMINAL_ENV", "").strip().lower() == "local":
            available.add("host_repo")
        return available


def validate_assignment(
    assignee: str | None, *, requires=None, workspace_kind: str = "scratch",
    workspace_path=None, skills=None, require_declaration: bool = False, check_profile: bool = True,
) -> list[str]:
    """Return canonical, deduplicated requirements or raise CapabilityMismatchError.

    Explicit requirements augment workspace inference; even ``requires=[]`` cannot
    disable worktree/dir checks. Unconstrained scratch tasks retain legacy behavior
    without reading a profile config. ``workspace_path`` is not probed: locality is
    established solely by backend policy, not a path existing on the dispatch host.

    Skill-derived requirements come only from a pinned skill's structured activation metadata
    (:func:`skill_requirements`); names/prose are never guessed into requirements. They augment
    ``requires`` like workspace inference does, so ``requires=[]`` cannot disable them. [daveey]

    ``require_declaration=True`` (agent create surfaces, when ``kanban.require_capability_declaration``
    is on) first refuses an omitted ``requires`` (None) that nothing structured backs, with
    :class:`RequiresDeclarationMissing`; an explicit ``[]`` passes. [daveey]

    ``check_profile=False`` (a card whose ``runs_on`` rules this machine out) validates only the shape and
    returns the declared requirements: this machine's profile of that name, and the skills it resolves, say
    nothing about the landing machine's. [daveey]
    """
    if require_declaration:
        check_declaration(requires, workspace_kind, skills=skills, assignee=assignee)
    required = _required_capabilities(requires, workspace_kind)
    if not check_profile:
        return required
    if skills:
        extra = skill_requirements(skills, assignee)
        if extra:
            merged = set(required) | set(extra)
            required = [n for n in _CAPABILITIES if n in merged] + [n for n in required if n not in _CAPABILITIES]
    if not any(name in _CAPABILITIES for name in required) or assignee is None:
        # Unassigned drafts cannot dispatch; assign_task validates before binding.
        return required
    if assignee in queue_assignees():
        # Declared parking name (kanban.queue_assignees): no profile, never spawned. [daveey]
        return required
    try:
        available = _profile_capabilities(assignee, required)
    except Exception as exc:
        raise CapabilityMismatchError(assignee, [n for n in required if n in _CAPABILITIES],
                                      reason=f"profile policy unavailable: {exc}") from exc
    missing = [name for name in required if name in _CAPABILITIES and name not in available]
    if missing:
        raise CapabilityMismatchError(assignee, missing)
    return required
