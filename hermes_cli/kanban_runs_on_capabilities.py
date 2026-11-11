"""``requires`` checked against the machines a card's ``runs_on`` can land on. [daveey]

:func:`hermes_cli.kanban_capabilities.validate_assignment` reads the assignee's profile on THIS machine. A
card with ``runs_on`` runs on another machine, with that machine's profile of the same name, so the local
check proves nothing about where it will run. Three pieces close that gap:

* :func:`runs_elsewhere` (create): a card whose ``runs_on`` rules this machine out skips ``create_task``'s
  checks of this machine's profiles (tools, pinned skills, model registry, provider binding). A missing or
  different local profile of that name never refuses it; the two checks below decide.

* :func:`check` (create, ``kanban move``): every Board-registry machine whose labels satisfy ``runs_on`` (and,
  for a move, is not this one) is a candidate. A candidate's profile tools are known for this machine (local
  policy) and for a gateway that publishes ``profile-tools:<profile>:<tool>`` in its hermes-hub capability
  record (:func:`hermes_cli.kanban_placement.target_info`). When every candidate is KNOWN to lack a profile
  tool the card needs, the write is refused with :class:`PlacementCapabilityMismatch`, which names each
  machine and what it lacks. A candidate whose tools are unknown (it publishes none, or the registry is
  unreachable) never refuses: the claim gate below is the backstop.
* :func:`claim_refusal` (worker-node pickup): the machine about to copy a foreign card checks its own
  profile. A card it cannot satisfy is left on the Board for a machine that can, so it never runs a card it
  could not hand back.

Only the profile tools a remote record can describe count (terminal, file, git, kanban); ``host_repo`` means a
checkout on the creator and dir/worktree cards never leave it.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Mapping, Optional

from hermes_cli.kanban_capabilities import CapabilityMismatchError

_log = logging.getLogger(__name__)

PROFILE_TOOLS = ("terminal", "file", "git", "kanban")
_logged_refusals: set[tuple[str, str]] = set()


class PlacementCapabilityMismatch(CapabilityMismatchError):
    """No machine ``runs_on`` can land on gives the assignee's profile the tools ``requires`` names."""

    code = "runs_on_capability_mismatch"

    def __init__(self, assignee: str, runs_on: list[str], lacking: Mapping[str, list[str]]):
        self.runs_on = list(runs_on)
        self.lacking = {name: list(missing) for name, missing in sorted(lacking.items())}
        missing = sorted({c for v in self.lacking.values() for c in v}, key=PROFILE_TOOLS.index)
        per_machine = "; ".join(f"machine {name} lacks {', '.join(v)}" for name, v in self.lacking.items())
        super().__init__(assignee, missing, reason=(
            f"runs_on={self.runs_on}: {per_machine} for profile {assignee!r}. Drop a requirement the work does "
            "not need, or choose runs_on for a machine whose profile has it"))


def _remote_tools(name: str) -> Optional[dict[str, set[str]]]:
    """``{profile: {tool}}`` gateway ``name`` publishes through hermes-hub, or None when unknown."""
    try:
        from hermes_cli import kanban_placement as kp
        return kp.target_info(name, now=time.time()).get("tools")
    except Exception:  # health: allow BLE001 -- unreadable replica: unknown, never a refusal
        return None


def _local_tools(assignee: str) -> Optional[set[str]]:
    try:
        from hermes_cli.kanban_capabilities import _profile_capabilities
        return set(_profile_capabilities(assignee))
    except Exception:  # health: allow BLE001 -- unreadable policy: unknown (create's own check refuses it)
        return None


def candidates(runs_on: list[str], machines: Mapping[str, Mapping[str, Any]], *,
               me: str, exclude_me: bool) -> list[str]:
    """Active, unheld registry machines carrying every ``runs_on`` label."""
    from hermes_cli import kanban_worker_node as wn
    out = []
    for name, entry in machines.items():
        if (exclude_me and name == me) or not entry or entry.get("state", "active") != "active" or entry.get("hold"):
            continue
        if set(runs_on) <= wn.effective_labels(entry, name):
            out.append(name)
    return sorted(out)


def missing_on(name: str, assignee: str, required: list[str], *, me: str,
               remote_tools: Callable[[str], Optional[dict]]) -> Optional[list[str]]:
    """Tools ``assignee`` lacks on machine ``name``: ``[]`` capable, None unknown."""
    held: Optional[set[str]]
    if name == me:
        held = _local_tools(assignee)
    else:
        tools = remote_tools(name)
        if tools is None:
            return None
        held = {str(t) for t in (tools.get(assignee) or ())}
        if "terminal" in held:
            held.add("git")  # the capability model: git means terminal access
    if held is None:
        return None
    return [c for c in required if c not in held]


def check(runs_on: Optional[list[str]], *, assignee: Optional[str], requires, workspace_kind: str = "scratch",
          me: str, machines: Optional[Mapping[str, Mapping[str, Any]]] = None, exclude_me: bool = False,
          remote_tools: Optional[Callable[[str], Optional[dict]]] = None) -> dict[str, Optional[list[str]]]:
    """``{machine: missing | None}`` for the candidates, or raise :class:`PlacementCapabilityMismatch` when every
    candidate is known to lack something. Empty when there is nothing to check (no ``runs_on``, no assignee, no
    profile tool required, registry unreachable, no candidate machine: other guards own those cases)."""
    if runs_on is None or not assignee:
        return {}
    from hermes_cli.kanban_capabilities import _required_capabilities
    try:
        required = [c for c in _required_capabilities(requires, workspace_kind) if c in PROFILE_TOOLS]
    except ValueError:
        return {}  # malformed requires: create's own validation reports it
    if not required:
        return {}
    if machines is None:
        from hermes_cli import kanban_worker_node as wn
        try:
            reg = wn.registry()
        except Exception:  # health: allow BLE001 -- registry unreachable: the claim gate decides
            reg = None
        if not reg:
            return {}
        machines = reg.get("machines") or {}
    names = candidates(list(runs_on), machines or {}, me=me, exclude_me=exclude_me)
    if not names:
        return {}
    fetch = remote_tools or _remote_tools
    verdicts = {n: missing_on(n, assignee, required, me=me, remote_tools=fetch) for n in names}
    if any(v is None or not v for v in verdicts.values()):
        return verdicts
    raise PlacementCapabilityMismatch(assignee, list(runs_on), {n: v for n, v in verdicts.items() if v})


def check_create(conn, runs_on, *, assignee: Optional[str], requires, workspace_kind: str = "scratch") -> None:
    """:func:`check` for ``create_task`` on a Board primary: the local check read THIS machine's profile."""
    if runs_on is None:
        return
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    if sync.board_mode(conn) != "primary":
        return
    labels = wn.normalize_runs_on(runs_on if isinstance(runs_on, str) else list(runs_on))
    try:
        reg = wn.registry() if labels else None
    except Exception:  # health: allow BLE001 -- same unknown-registry policy as check/runs_elsewhere
        reg = None
    machines = reg.get("machines") if reg is not None else None
    me = wn._own(conn)
    # No candidates is not evidence of missing profile tools (check deliberately
    # allows unknown tools). It IS evidence of an impossible label conjunction.
    # Include held/inactive machines: temporary unavailability is not a typo.
    # A missing self entry is incomplete evidence, as in runs_elsewhere.
    if labels and machines and machines.get(me) and not any(
            set(labels) <= wn.effective_labels(entry, name) for name, entry in machines.items()):
        raise ValueError(
            f"runs_on={labels}: no enrolled machine carries all these labels. Nothing was written. "
            "Use placement='<gateway>' to name a gateway, or runs_on=['machine:<registered-name>'] "
            "to pin a Board machine; bare strings are capability labels, not gateway aliases.")
    check(labels, assignee=assignee, requires=requires, workspace_kind=workspace_kind, me=wn._own(conn),
          machines=machines)


def runs_elsewhere(conn, runs_on, *, machines: Optional[Mapping[str, Mapping[str, Any]]] = None) -> bool:
    """True when ``runs_on`` on a Board primary rules THIS machine out, so the card can only run with another
    machine's profile of the assignee's name. ``create_task`` then skips its checks of this machine's
    profiles (tools, skills, model registry, provider binding) and leaves them to :func:`check_create`, the
    landing dispatcher and :func:`claim_refusal`.

    False (keep the local checks) for no ``runs_on``, ``[]`` (any machine), a board that is not a primary, or
    labels the registry cannot settle (unreachable, empty, or missing this machine's entry).
    A ``machine:<other>`` label needs no registry: this
    machine only carries ``machine:<its own name>``."""
    if runs_on is None:
        return False
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    if sync.board_mode(conn) != "primary":
        return False
    labels = wn.normalize_runs_on(runs_on if isinstance(runs_on, str) else list(runs_on)) or []
    if not labels:
        return False
    me = wn._own(conn)
    if any(lab.startswith("machine:") and lab != f"machine:{me}" for lab in labels):
        return True
    pool = machines
    if pool is None:
        try:
            reg = wn.registry()
        except Exception:  # health: allow BLE001 -- registry unreachable: unknown, keep the local checks
            reg = None
        if reg is None:
            return False
        pool = reg.get("machines") or {}
    # No self entry means unknown labels, not evidence that this machine lacks them.
    if not pool.get(me):
        return False
    return not set(labels) <= wn.effective_labels(pool[me], me)


def verdict_warning(implementer: str, task) -> dict:
    """``{"capability_warning": ...}`` for a ``changes_requested`` event, or ``{}``.

    The implementer already ran the card, so a capability it lacks on the machine recording the verdict
    (another machine's profile of that name, or policy drift since) is a warning on the verdict, never a
    refusal: refusing turned a reviewer's verdict on a worker-node card into a human ask
    (t_01a184ac93d31979)."""
    from hermes_cli.kanban_capabilities import validate_assignment
    try:
        validate_assignment(implementer, requires=task.requires, workspace_kind=task.workspace_kind,
                            workspace_path=task.workspace_path, skills=task.skills)
    except CapabilityMismatchError as exc:
        return {"capability_warning": {"profile": implementer, "missing": list(exc.missing), "error": str(exc)[:400]}}
    return {}


def _loads(value: Any, default):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def claim_refusal(card: Mapping[str, Any], *, me: str = "") -> Optional[str]:
    """None when this machine's profile can run ``card`` (a replica row), else why not.

    The same check create runs locally, applied by the machine that would run the card. Logged once per
    card and reason so a card no machine can take is visible in the worker's log. Pinned skills are not a
    pickup gate: a worker that cannot load one runs with the missing-skill note (``missing_pin_note``), so a
    remote card whose create skipped the skill check is never stranded on the Board. [daveey]"""
    from hermes_cli.kanban_placement_coordination import claim_refusal as coordination_refusal
    if refusal := coordination_refusal(card, me):
        return refusal
    assignee = card.get("assignee")
    if not assignee:
        return None
    from hermes_cli.kanban_capabilities import validate_assignment
    try:
        validate_assignment(assignee, requires=_loads(card.get("requires"), []) or [],
                            workspace_kind=card.get("workspace_kind") or "scratch",
                            skills=_loads(card.get("skills"), None) or None)
    except CapabilityMismatchError as exc:
        reason = str(exc)
    except ValueError as exc:  # a requirement this core does not know: it cannot prove it either
        reason = f"cannot check requires: {exc}"
    else:
        return None
    key = (str(card.get("id")), reason)
    if key not in _logged_refusals:
        _logged_refusals.add(key)
        _log.info("kanban worker node: not claiming %s on %s, left for a capable machine: %s",
                  card.get("id"), me or "this machine", reason)
    return reason
