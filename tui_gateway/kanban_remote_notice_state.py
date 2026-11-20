"""Identity shared by remote notice arrival, claims and stale projections."""
from hermes_cli.kanban_withdrawal_notice import envelope


def state(task: dict) -> str:
    notice = envelope(task)
    if notice is not None:
        return f"request_withdrawn@{notice['event_id']}"
    from hermes_cli.kanban_remote_ask import envelope as ask_envelope
    from tui_gateway.kanban_remote_notices import drop_mark
    if (ask := ask_envelope(task)) is not None and drop_mark(task) is None:
        return f"{ask['kind']}@{ask['event_id']}"
    from tui_gateway.kanban_remote_notices import _terminal_at
    at = _terminal_at(task) if task.get("status") in ("done", "blocked") else None
    return f"{task['status']}@{int(at) if at else 0}"
