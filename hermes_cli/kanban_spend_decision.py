"""Durable decision hold and producer for a cumulative spend stop. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_db as kb

EVENT = "spend_budget_triaged"
NOTICE = "spend_budget_decision_ready"


def ensure_producer(conn, task_id: str) -> None:
    """Repair pre-notice stops too; one durable producer receipt per spend-stop episode."""
    import json
    from hermes_cli.kanban_sync_divergence import _event_order
    with kb.write_txn(conn, allow_nested=True):
        if not pending(conn, task_id):
            return
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind=?", (task_id, EVENT))]
        stop = max(events, key=_event_order)
        episode = stop.get("uid") or str(stop["id"])
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? "
                        "AND json_extract(payload,'$.episode')=?", (task_id, NOTICE, episode)).fetchone():
            return
        payload = json.loads(stop["payload"] or "{}")
        route = producer(conn, task_id, payload.get("reason") or "Spend budget reached")
        kb._append_event(conn, task_id, NOTICE, {**payload, "episode": episode, "producer": route})



def safe_ensure_producer(conn, task_id: str) -> None:
    import logging
    try:
        ensure_producer(conn, task_id)
    except Exception:  # health: allow BLE001 -- retain the spend hold; the next sweep retries producer publication
        logging.getLogger(__name__).exception("spend decision producer failed for %s; hold retained", task_id)


def pending(conn, task_id: str) -> bool:
    """Only an explicit promotion or archive releases a spend stop, not an automatic status change."""
    from hermes_cli.kanban_sync_divergence import _event_order
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id=? "
        "AND kind IN ('spend_budget_triaged','promoted_manual','archived') "
        "AND created_at=(SELECT MAX(created_at) FROM task_events WHERE task_id=? "
        "AND kind IN ('spend_budget_triaged','promoted_manual','archived'))", (task_id, task_id)).fetchall()
    return bool(rows and max((dict(r) for r in rows), key=_event_order)["kind"] == EVENT)


def notice(payload: dict) -> str:
    return (str(payload.get("reason") or "Spend budget reached") + ". Held in triage until a recorded decision: "
            "split or re-scope the work, then manually promote it, or archive it. "
            "Do not requeue unchanged; the stale-triage sweep cannot release this hold.")


def producer(conn, task_id: str, note: str) -> dict:
    """Inside the stop transaction: one chat-notice route OR one supervisor, never both."""
    from hermes_cli.kanban_db_notify import is_headless_session
    from hermes_cli.profiles import get_active_profile_name

    def bind(platform, chat_id):
        conn.execute(
            "INSERT OR IGNORE INTO kanban_notify_subs "
            "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
            "VALUES (?,?,?,'','notify','{\"reason\":\"origin\"}',unixepoch(),"
            "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?))",
            (task_id, platform, chat_id, task_id))
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_triage_sweep import _supervisor_profile

    task = kb.get_task(conn, task_id)
    assert task is not None
    # Bind legacy origin sessions before appending the notice so its cursor cannot skip this event.
    if task.session_id and not is_headless_session(task.session_id):
        bind("tui", task.session_id)
    own = gateway_names()[0]
    links = (_chat_links(conn, task_id) if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone() else [])
    for link in links:
        session = link["chat_session"]
        gw, profile = link["chat_gateway"], link["chat_profile"] or "default"
        chat_id = f"{gw}/{profile}/{session}"
        if not is_headless_session(chat_id):
            local = gw == own and profile == get_active_profile_name()
            bind("tui" if local else "hub", session if local else chat_id)
    from hermes_cli.kanban_db_notify import _owning_sub_clause
    if conn.execute("SELECT 1 FROM kanban_notify_subs s JOIN tasks p ON p.id=s.task_id "
                    f"WHERE s.task_id=? AND {_owning_sub_clause()} LIMIT 1", (task_id,)).fetchone():
        return {"kind": "chats"}
    episode = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0]
    sup = kb.create_task(
        conn, title=f"Spend budget decision: {task.title}"[:200],
        body=f"Card {task_id} ({task.title}) has stopped. No chat owns its decision.\n\n" + notice({"reason": note})
             + f"\n\nRecord the decision on {task_id}; use `hermes kanban promote {task_id} --reason '<what changed>'` "
               "only after re-scoping or justifying further spend, or archive it. Do not run the original work here.",
        assignee=_supervisor_profile(), created_by="kanban-spend-budget", workspace_kind="scratch",
        idempotency_key=f"spend-budget-decision:{task_id}:{episode}", tenant=task.tenant)
    return {"kind": "supervisor", "task_id": sup}
