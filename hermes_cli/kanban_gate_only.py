"""Gate-only assignees: profiles only an external admission gate may hand a card to. [daveey]

``kanban.gate_only_assignees`` maps such a profile to the queue assignee a new card waits under instead,
e.g. ``{bb-repair-native: bb-queue}`` on the mini's bb board. The gate (bb_gate) admits a queued card by
reassigning it (``assign_task``), never by creating one, so a create naming the gate-only profile skips
admission: the dispatcher spawns the native worker at once, the gate reclaims it as unauthorized and
records ``native_rejected``, and the card is stranded until a manual renewal (five-whys
t_01a1d08fbacf4432: 9 cards since 10-28, 8 by worker ``kanban_create``, 1 by the resume requester).

So :func:`refuse_create` refuses every create naming one (the ``kanban_create`` tool and ``hermes kanban
create`` both reach it through ``create_task``), and :func:`create_assignee` lets a kernel producer that
copies an existing card's assignee (the resume requester) file under the queue assignee instead.
Reassignment (``assign_task``) is untouched. Read like ``queue_assignees``: the active profile's config
and the board home's config.
"""
from __future__ import annotations


class GateOnlyAssigneeError(ValueError):
    """A create named an assignee only the admission gate may assign."""

    def __init__(self, assignee: str, queue: str):
        self.assignee, self.queue = assignee, queue
        super().__init__(
            f"assignee {assignee!r} is gate-only (kanban.gate_only_assignees): only its admission gate may "
            f"assign it, by reassigning a queued card. Create the card with assignee {queue!r} instead; "
            "the gate admits it from there.")


def gate_only_assignees() -> dict[str, str]:
    """``{gate-only profile: queue assignee}`` from config. Never raises; malformed entries are ignored."""
    names: dict[str, str] = {}

    def _add(cfg) -> None:
        raw = ((cfg or {}).get("kanban") or {}).get("gate_only_assignees")
        if not isinstance(raw, dict):
            return
        for name, queue in raw.items():
            if isinstance(name, str) and name.strip() and isinstance(queue, str) and queue.strip():
                names[name.strip()] = queue.strip()

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
    return names


def refuse_create(assignee) -> None:
    """Raise :class:`GateOnlyAssigneeError` when a create names a gate-only assignee."""
    if not isinstance(assignee, str) or not assignee:
        return
    queue = gate_only_assignees().get(assignee.strip())
    if queue:
        raise GateOnlyAssigneeError(assignee.strip(), queue)


def create_assignee(assignee):
    """The assignee a kernel-made card copying *assignee* is created under: its queue when gate-only."""
    if not isinstance(assignee, str) or not assignee:
        return assignee
    return gate_only_assignees().get(assignee.strip(), assignee)
