"""Audit-only identity for lifecycle calls; never an authorization signal."""
import os


def _agent_actor(*, session="", worker="") -> str:
    if session:
        return f"agent (chat {session})"
    return f"agent (task {worker})" if worker else "agent"


def cli_actor() -> str:
    """Distinguish a tool subprocess from a human shell without guessing from profile."""
    from hermes_cli import kanban

    session = (os.environ.get("HERMES_SESSION_KEY") or os.environ.get("HERMES_SESSION_ID") or "").strip()
    worker = os.environ.get("HERMES_KANBAN_WORKER_TASK") or os.environ.get("HERMES_KANBAN_TASK")
    if session or worker or kanban._agent_invoked():
        return _agent_actor(session=session, worker=worker)
    return "user"


def tool_actor(*, worker_task=None) -> str:
    """Tools are always agents; an already-guarded owning task takes precedence.

    Chat identity is request-scoped: another chat may have set process env.
    The caller supplies worker_task only after its lifecycle ownership guard.
    """
    from gateway.session_context import get_session_env

    if worker_task:
        return _agent_actor(worker=worker_task)
    session = (get_session_env("HERMES_SESSION_ID") or get_session_env("HERMES_SESSION_KEY") or "").strip()
    return _agent_actor(session=session)
