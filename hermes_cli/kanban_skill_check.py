"""Create-time check of a card's pinned skills against the ASSIGNEE profile. [daveey]

A pinned skill the assignee cannot load used to be accepted at create time and only fail when the
worker started (``Error: Unknown skill(s): x``), after which the dispatcher retried a run that could
never succeed. The check uses the worker's own lookup (``build_preloaded_skills_prompt``: same name
normalization, disabled list and external dirs) under the assignee's home, so "refused here" and
"missing in the worker" are the same answer.
"""

from __future__ import annotations

import difflib
from typing import Iterable, Optional


class UnknownSkillError(ValueError):
    """A create refused because the assignee profile cannot load a pinned skill."""

    code = "unknown_skill"

    def __init__(self, message: str, *, assignee: str, missing: list[str], suggestions: dict[str, list[str]]):
        super().__init__(message)
        self.assignee, self.missing, self.suggestions = assignee, missing, suggestions


def _assignee_home(assignee: str) -> Optional[str]:
    """The home the dispatcher would run the assignee's worker under, or None when it has none
    (an unknown assignee is the assignment checks' problem, not this one's)."""
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _unresolved(skills: list[str], home: str) -> tuple[list[str], list[str]]:
    """(missing labels, loadable names) for *skills* as a worker under *home* would see them."""
    from agent.skill_commands import build_preloaded_skills_prompt
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.skills_tool import find_all_skills
    token = set_hermes_home_override(home)
    try:
        _prompt, _loaded, missing = build_preloaded_skills_prompt(skills)
        names = [s["name"] for s in find_all_skills()] if missing else []
    finally:
        reset_hermes_home_override(token)
    return missing, names


def missing_pin_note(missing: list[str]) -> str:
    """The prompt note a dispatcher-owned kanban worker starts with when pinned skills did not load,
    or "" outside such a worker (an interactive ``hermes -s typo`` keeps failing loudly). Exiting
    instead only made the dispatcher retry a run that could never load them (#119619)."""
    import os

    from agent.delegation_context import is_dispatcher_owned_worker_context
    if not missing or not os.environ.get("HERMES_KANBAN_TASK") or not is_dispatcher_owned_worker_context():
        return ""
    from agent.skill_commands import format_missing_skills
    return (f"[Kanban: this card pins skills this profile could not load ({format_missing_skills(missing)}). "
            "Work without them if the task allows it; if it cannot be done without them, call kanban_block "
            "with kind='assignment' naming the missing skill.]")


def check_task_skills(skills: Optional[Iterable[str]], assignee: Optional[str], *, enabled: bool = True) -> None:
    """Raise :class:`UnknownSkillError` when *assignee* (unset: the default ready assignee) cannot
    load one of *skills*. ``enabled=False`` is a no-op (internal creates that skip the check).

    Plugin-qualified names (``plugin:skill``) are left to the worker: they resolve through the
    plugin registry of the worker's own process, which this process may not have loaded.
    """
    names = [s for s in (skills or ()) if s and ":" not in s]
    if not enabled or not names:
        return
    if not assignee:
        from hermes_cli.kanban_db import _default_ready_assignee
        assignee = _default_ready_assignee()
    home = _assignee_home(assignee)
    if home is None:
        return
    missing, available = _unresolved(names, home)
    if not missing:
        return
    from agent.skill_commands import format_missing_skills
    suggestions = {m: difflib.get_close_matches(m, available, n=3, cutoff=0.6) for m in missing}
    hints = "; ".join(f"{m!r}: did you mean {', '.join(s)}?" for m, s in suggestions.items() if s)
    raise UnknownSkillError(
        f"{format_missing_skills(missing)} for assignee profile {assignee!r}: its worker could not load "
        f"{'it' if len(missing) == 1 else 'them'}." + (f" {hints}" if hints else "")
        + " Fix the name (`hermes -p <assignee> skills list`), drop it, or assign a profile that has it. "
          "Nothing was created.",
        assignee=assignee, missing=missing, suggestions=suggestions)
