"""``process_manage(action="list")``: this chat's background processes. [daveey]"""

from contextlib import suppress


def list_processes(task_id) -> dict:
    """Processes of *task_id*, plus session-scoped ones (e.g. a forgotten preview server): they share
    the gateway session_key and can block session reset."""
    from tools.process_registry import _redact_process_result, process_registry
    session_key = ""
    with suppress(Exception):
        # See #29177.
        from tools.approval_context import get_current_session_key
        session_key = get_current_session_key(default="") or ""
    return {"processes": [
        _redact_process_result(p)
        for p in process_registry.list_sessions(
            task_id=task_id, session_key=session_key or None, include_retained=True)]}
