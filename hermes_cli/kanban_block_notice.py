"""Read-only actionable block presentation shared by notification entry paths."""
import json

from hermes_cli.kanban_human_request import needs_human


def blocked_actor_fields(conn, task_id: str) -> dict:
    """Publish audit provenance with a remote blocked card, not its human action owner."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    payload = json.loads(row[0] or "{}") if row else {}
    actor = payload.get("actor")
    return {"block_actor": actor} if isinstance(actor, str) and actor else {}


def block_notice(task, task_id: str, board: str, *, gateway: str = "", reason: str = "", actor: str = "") -> str:
    """Use current card evidence, never event prose, to assign a human action.

    Links open the existing task flow; this function grants no authority and
    never interprets evidence or action strings as executable commands.
    """
    def get(key, default=None):
        return task.get(key, default) if isinstance(task, dict) else getattr(task, key, default)

    link = f"@task:{board or 'default'}{'@' + gateway if gateway else ''}/{task_id}"
    status = get("status")
    if status and status not in {"blocked", "triage"}:
        return f"Earlier block resolved; current state: {status}.\nOpen task: {link}"
    request = get("human_request")
    if isinstance(request, str):
        try:
            request = json.loads(request)
        except ValueError:
            request = None
    if status in {"blocked", "triage"} and isinstance(request, dict) and needs_human(request, kind=get("block_kind")):
        lines = [f"blocked — actor: {actor or 'user'}", f"Next action for the user: {request['action']}",
                 f"Why only the user: {request['why_human']}", f"Scope: {request['scope']}",
                 f"Open task: {link}"]
        if request["interaction"] == "user_terminal":
            lines.append("Use the owner Desktop session's user-terminal flow; opening this task or unblocking it does not execute a command or grant terminal authority.")
        return "\n".join(lines)
    owner = get("assignee") or "assigned agent"
    return (f"blocked — actor: {actor or f'agent ({owner})'}; no actionable human request recorded.\n"
            "Next action: the agent must resolve the execution/dependency failure or prepare a concrete human action with evidence.\n"
            f"Reason: {reason or get('block_kind') or 'block details missing'}\nOpen task: {link}")
