"""Kanban write fence refusal: the durable invariant below the tool/CLI fast-fail guards. [daveey]

The tool/CLI guards are UX, not a trust boundary (a child can shell out or import the DB
module), so ``kanban_db._assert_not_delegated_child_mutation`` delegates here from every
``write_txn`` user and board-metadata mutator.

The refusal text names the fence that applied. An env-inherited fence covers every terminal,
background and execute_code subprocess of a Kanban worker, the worker's own terminal included,
with or without delegate_task. Naming only delegate_task there made a worker read its own
terminal fence as a delegate child's lineage leaking into its shell (t_01a187533767dc4e).
"""
from __future__ import annotations

import os
from pathlib import Path

_CHILD_REFUSAL = "delegate_task child contexts cannot mutate Kanban tasks or boards"
_DESCENDANT_HEAD = "Kanban worker and delegate_task descendant processes cannot mutate Kanban tasks or boards"
# A worker asked for a live tool call (a refusal, a block) on a throwaway board had no route and handed the
# command back to the user (defect agent-cbb2687c75). The fence covers the lineage's board root only. [daveey]
SCRATCH_HOME_HINT = (
    "For a live probe of kanban_* tools or board writes, use a throwaway HERMES_HOME outside the fenced "
    "root (not TMPDIR, which is under ~/.hermes): H=$(mktemp -d ~/kanban-probe.XXXX); unset HERMES_KANBAN_DB "
    "HERMES_KANBAN_BOARD HERMES_KANBAN_HOME; HERMES_HOME=$H python3 <probe>; then rm -rf \"$H\"."
)


def assert_not_fenced(path: str | Path) -> None:
    """Raise ``PermissionError`` when Kanban mutations at *path* are fenced for this process."""
    from agent.delegation_context import is_delegated_child_context, kanban_path_is_fenced

    if not kanban_path_is_fenced(path):
        return
    if is_delegated_child_context():
        raise PermissionError(_CHILD_REFUSAL)
    from hermes_cli.top_level_env import leaked_worker_env_diagnosis
    try:
        diagnosis = leaked_worker_env_diagnosis()
    except Exception:  # health: allow BLE001 -- diagnosis is best-effort text; the refusal must still raise
        diagnosis = None
    if diagnosis:
        raise PermissionError(f"{_DESCENDANT_HEAD}. {diagnosis}")
    raise PermissionError(descendant_refusal_text())


def descendant_refusal_text() -> str:
    """Refusal for a process fenced by the inherited ``HERMES_DELEGATED_CHILD_CONTEXT`` marker."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, DELEGATED_CHILD_OWNER_ENV, KANBAN_WORKER_TASK_ENV

    owner = (os.environ.get(DELEGATED_CHILD_OWNER_ENV) or "").split(":", 1)[0] or "unknown"
    card = (os.environ.get(KANBAN_WORKER_TASK_ENV) or "").strip()
    whose = f"Kanban worker for {card}" if card else "Kanban worker or delegate_task child"
    return (
        f"{_DESCENDANT_HEAD}: this process inherited {DELEGATED_CHILD_ENV_MARKER} from agent pid {owner} "
        f"(the {whose}). Every terminal, background and execute_code subprocess of a Kanban worker is "
        "write-fenced by design, the worker's own terminal included, with or without delegate_task; it "
        "is not a leak. Make the change with the agent's kanban_* tools, or ship board-repair logic as "
        "code the gateway/dispatcher runs. " + SCRATCH_HOME_HINT
    )
