"""Capsule-bound worker view of its own card for ``kanban_show`` (:mod:`tools.kanban_show`). [daveey]"""
import os


def own_card_view(task, tid: str, worker_context: str) -> tuple[str, dict]:
    """``(worker_context, extra fields)``. Unbound workers and other cards: the context as built, no extras.

    A capsule worker's own card row names the HOST ``workspace_path``, which the sandbox cannot see; the
    Workspace header is rewritten to the capsule's (same rewrite as its first turn) and
    ``execution_workspace`` says where the task's checkout actually is."""
    from tools import capsule_workspace
    if not (capsule_workspace.bound() and tid == os.environ.get("HERMES_KANBAN_TASK")):
        return worker_context, {}
    from gateway.session_kanban_capsule import capsule_worker_context
    rewritten = capsule_worker_context({"context": worker_context, "workspace": task.workspace_path})
    return rewritten, {"execution_workspace": capsule_workspace.CAPSULE_CWD}
