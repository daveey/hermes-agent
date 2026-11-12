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


def _actor_session(actor) -> str:
    """The chat session id an ``agent (chat <id>)`` audit label names, else ``""``."""
    if not isinstance(actor, str) or not actor.startswith("agent (chat ") or not actor.endswith(")"):
        return ""
    return actor[len("agent (chat "):-1].strip()


def actor_is_chat(actor, chat_id) -> bool:
    """Whether the audit actor is the subscriber chat ``chat_id``.

    The actor carries either the raw session id or the gateway session key
    (``agent:<ns>:<platform>:dm:<id>``); subscriptions store the bare ``<id>``.
    Only DM keys are reduced to their chat id: group/thread keys append thread
    and participant segments, so they never match by suffix. [daveey]
    """
    session, chat = _actor_session(actor), str(chat_id or "").strip()
    if not session or not chat:
        return False
    if session == chat:
        return True
    parts = session.split(":")
    return len(parts) >= 5 and parts[0] == "agent" and parts[3] == "dm" and ":".join(parts[4:]) == chat


def is_metadata_only(payload) -> bool:
    """A ``blocked`` event that amended a parked card's ask without a new block transition."""
    return isinstance(payload, dict) and payload.get("metadata_only") is True


def ask_update_notice(task, task_id: str, board: str, payload, *, chat_id: str | None = "", gateway: str = "") -> str | None:
    """Notice text for a metadata-only ask amendment; ``None`` = silent for this chat.

    The chat that made the edit already knows it, so it gets nothing. Other
    subscribers get one line with the new action, not a fresh "blocked —"
    notice: the card did not block again. [daveey]
    """
    if actor_is_chat(payload.get("actor"), chat_id):
        return None

    def get(key, default=None):
        return task.get(key, default) if isinstance(task, dict) else getattr(task, key, default)

    status = get("status")
    if status and status not in {"blocked", "triage"}:
        return block_notice(task, task_id, board, gateway=gateway, reason=str(payload.get("reason") or ""),
                            actor=str(payload.get("actor") or ""))
    request = payload.get("human_request")
    action = request.get("action") if isinstance(request, dict) else None
    link = f"@task:{board or 'default'}{'@' + gateway if gateway else ''}/{task_id}"
    text = " ".join(str(action).split()) if action else "no human action recorded"
    return f"ask updated (card still blocked): {text} — open task: {link}"


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
