"""Delegate-child / worker-subprocess write fence for the ``kanban_*`` tools.

Lives outside ``kanban_tools`` so that file, already over its line cap, does not grow. ``kanban_tools``
re-exports both names, so callers and patches keep working. [daveey]

The tool fence mirrors the DB fence (``kanban_path_is_fenced``): a worker's terminal/execute_code
subprocess (fenced only by the inherited ``HERMES_DELEGATED_CHILD_CONTEXT=<root>`` marker) is refused
board-local writes to the pinned board and boards under that root, but may drive the real tool logic
on a throwaway ``HERMES_HOME`` outside it (defect agent-cbb2687c75: a worker asked for a live refusal
had no route and handed an ssh command back to the user).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _env_fence_covers_board(board: Optional[str]) -> bool:
    """Whether a process fenced only by the inherited env marker is fenced for *board*.

    The home must lie outside the fence as well as the board, so the call runs with the scratch home's
    (empty) gateway config and credentials, never the real ones. Fails closed: an in-process delegate
    child, a legacy ``"1"`` or missing marker, or an unresolvable path."""
    from tools import kanban_tools as kt
    if kt._is_delegated_child_context():
        return True
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    marker = (os.environ.get(DELEGATED_CHILD_ENV_MARKER) or "").strip()
    if not marker or marker == "1":
        return True
    try:
        from agent.delegation_context import kanban_path_is_fenced
        from hermes_cli import kanban_db as kb
        from hermes_constants import get_hermes_home
        return kanban_path_is_fenced(get_hermes_home()) or kanban_path_is_fenced(kb.kanban_db_path(board=board))
    except Exception:
        logger.debug("board fence check for %r failed; refusing", board, exc_info=True)
        return True


def _reject_delegated_child_mutation(tool_name: str, args: Optional[dict] = None) -> None:
    """A delegate_task child shares the parent's process, so inherited HERMES_KANBAN_*
    env is not proof of ownership: it may report findings but must not mutate.

    With *args* (a board-local write), a worker's subprocess fenced only by the env marker is refused
    only when ``args["board"]`` resolves to a fenced board. Without *args* it is refused everywhere
    (cross-gateway writes the local path check cannot cover)."""
    from tools import kanban_tools as kt
    if kt._delegation_ctx("is_delegated_child_process_context", False) and (
            args is None or _env_fence_covers_board(args.get("board"))):
        msg = (f"{tool_name} refused: delegate_task child agents are not Kanban run owners. "
               "Return findings to the parent agent; the dispatcher worker or an explicitly "
               "configured Kanban orchestrator must perform board mutations")
        if not kt._is_delegated_child_context():
            # Fenced by an inherited env marker, not an in-process child: a leaked worker env
            # (a Desktop reopened from a worker) must say so instead.
            from hermes_cli.kanban_db_fence import SCRATCH_HOME_HINT
            from hermes_cli.top_level_env import delegated_child_refusal
            msg = delegated_child_refusal(msg)
            if args is not None:  # board-scoped write: say how to reach an unfenced scratch board
                msg = f"{msg.rstrip('.')}. {SCRATCH_HOME_HINT}"
        raise kt._Reject(msg if msg.endswith(".") else msg + ".")
