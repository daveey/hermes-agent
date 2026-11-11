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


DROP_EVENTS = ("resolved_check_broken", "resolved_check_lost")
_DROP_CAUSE = {"resolved_check_broken": "its probe kept failing", "resolved_check_lost": "its probe went missing"}


def _drop_backfilled_origins(events):
    """Remove a historical drop event when a backfill re-emission in the same batch names it as its origin.

    Match stable UIDs across replicas. Integer references are safe only for UID-less,
    unsynced events; a sender's local ID must never suppress a different receiver event.
    """
    origins = set()
    local_origins = set()
    for e in events:
        payload = getattr(e, "payload", None)
        if e.kind in DROP_EVENTS and isinstance(payload, dict) and payload.get("backfilled") is True:
            uid = payload.get("origin_event_uid")
            if isinstance(uid, str) and uid:
                origins.add((uid, e.kind))
                continue
            if "origin_event_uid" in payload or getattr(e, "uid", None):
                continue
            try:
                local_origins.add((int(payload["origin_event_id"]), e.kind))
            except (KeyError, TypeError, ValueError):
                pass
    if not origins and not local_origins:
        return events
    return [e for e in events if (getattr(e, "uid", None), e.kind) not in origins
            and not (not getattr(e, "uid", None) and (int(e.id), e.kind) in local_origins)]


def coalesce_superseded_blocks(events):
    """Drop ``blocked`` events that a later drop event in the same unseen batch supersedes. [daveey]

    The drop notice renders the current card (block text plus the lost-detection warning), so the earlier
    ``blocked`` of the same batch would print the same marked card twice. Only blocks older than a drop event
    in this batch go; an ordinary block, or one re-filed after the drop, is kept. Callers keep the full claimed
    cursor range, so ack/rewind semantics are unchanged.
    """
    events = _drop_backfilled_origins(list(events))
    last_drop = max((int(e.id) for e in events if e.kind in DROP_EVENTS), default=None)
    if last_drop is None:
        return events
    return [e for e in events if not (e.kind == "blocked" and int(e.id) < last_drop)]


def drop_warning(request: dict, drop_event: str = "") -> str:
    """Notice head for an ask whose resolved_check was dropped. [daveey]

    The filed ask text may promise "I'll notice automatically"; that was true when it was filed and is not now,
    so the caller renders it under a label that makes it historical (persisted text is never edited).
    """
    cause = _DROP_CAUSE.get(str(request.get("resolved_check_drop_reason") or drop_event))
    return ("Automatic detection stopped" + (f" ({cause})" if cause else "") + ": this ask's resolved_check is no "
            "longer probed, so the card will NOT resume by itself when your step is done. It resumes only if you "
            "unblock it or the ask is re-filed with a working resolved_check.")


def block_notice(task, task_id: str, board: str, *, gateway: str = "", reason: str = "",
                 actor: str = "", drop_event: str = "") -> str:
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
        if request.get("resolved_check_dropped_at"):
            lines[1:2] = [drop_warning(request, drop_event),
                          "Your step is still needed. Filed ask, historical wording (any promise below to detect "
                          "it automatically no longer applies):", lines[1]]
            lines.insert(1, f"Work: {get('title') or task_id}")
        elif drop_event in DROP_EVENTS:  # re-filed since the drop: never report "stopped" as the current state
            lines.insert(1, "Automatic detection had stopped for an earlier version of this ask; it was re-filed "
                            "since, so this is the current ask.")
        if request["interaction"] == "user_terminal":
            lines.append("Use the owner Desktop session's user-terminal flow; opening this task or unblocking it does not execute a command or grant terminal authority.")
        return "\n".join(lines)
    owner = get("assignee") or "assigned agent"
    return (f"blocked — actor: {actor or f'agent ({owner})'}; no actionable human request recorded.\n"
            "Next action: the agent must resolve the execution/dependency failure or prepare a concrete human action with evidence.\n"
            f"Reason: {reason or get('block_kind') or 'block details missing'}\nOpen task: {link}")
