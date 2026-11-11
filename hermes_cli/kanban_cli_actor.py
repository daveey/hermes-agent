"""Audit-only identity for CLI lifecycle calls; never an authorization signal."""
import os


def cli_actor() -> str:
    """Distinguish a tool subprocess from a human shell without guessing from profile."""
    from hermes_cli import kanban

    session = (os.environ.get("HERMES_SESSION_KEY") or os.environ.get("HERMES_SESSION_ID") or "").strip()
    if session:
        return f"agent (chat {session})"
    worker = os.environ.get("HERMES_KANBAN_WORKER_TASK") or os.environ.get("HERMES_KANBAN_TASK")
    if worker:
        return f"agent (task {worker})"
    return "agent" if kanban._agent_invoked() else "user"
