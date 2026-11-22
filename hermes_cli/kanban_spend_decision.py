"""Durable decision hold and producer for a cumulative spend stop. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_model_neutral import forwarded_text, forwarded_title

EVENT = "spend_budget_triaged"
NOTICE = "spend_budget_decision_ready"


def ensure_producer(conn, task_id: str) -> None:
    """Repair pre-notice stops too; one durable producer receipt per spend-stop episode."""
    import json
    with kb.write_txn(conn, allow_nested=True):
        stop = pending_episode(conn, task_id)
        if stop is None:
            return
        episode = stop.get("uid") or str(stop["id"])
        previous = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? "
                                "AND json_extract(payload,'$.episode')=? ORDER BY id DESC LIMIT 1",
                                (task_id, NOTICE, episode)).fetchone()
        payload = json.loads(stop["payload"] or "{}")
        from hermes_cli.kanban_refusal_receipt import restricted
        safety = restricted(conn, task_id)
        prior = json.loads(previous[0]) if previous else None
        if prior and (not safety or prior.get('cause') == 'safety_refusal'):
            return
        if safety:
            payload['cause'] = 'safety_refusal'
        route = prior['producer'] if prior else producer(conn, task_id, payload.get('reason') or 'Spend budget reached')
        kb._append_event(conn, task_id, NOTICE, {**payload, "episode": episode, "producer": route})



def safe_ensure_producer(conn, task_id: str) -> None:
    import logging
    try:
        ensure_producer(conn, task_id)
    except Exception:  # health: allow BLE001 -- retain the spend hold; the next sweep retries producer publication
        logging.getLogger(__name__).exception("spend decision producer failed for %s; hold retained", task_id)


def pending_episode(conn, task_id: str):
    """Same existing episode as canonical Board, including safety annotations."""
    import json
    from hermes_cli.kanban_sync_divergence import _event_order
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind IN "
        "('spend_budget_triaged','decision_hold_restricted','promoted_manual','archived')", (task_id,))]
    events.sort(key=_event_order, reverse=True)
    for event in events:
        try:
            payload = json.loads(event['payload'] or '{}')
        except (TypeError, ValueError):
            continue
        if (isinstance(payload, dict) and payload.get('cause') == 'safety_refusal'
                and event['kind'] in (EVENT, 'decision_hold_restricted')):
            return next((e for e in events if e['kind'] == EVENT
                         and (e.get('uid') or str(e['id'])) == payload.get('episode')),
                        event) if event['kind'] != EVENT else event
    ordinary = [e for e in events if e['kind'] != 'decision_hold_restricted']
    return ordinary[0] if ordinary and ordinary[0]['kind'] == EVENT else None


def pending(conn, task_id: str) -> bool:
    """Budget promotion/archive never clear an attributable safety restriction."""
    return pending_episode(conn, task_id) is not None


def unblock_refusal(conn, task_id: str) -> str | None:
    """An unblock is not a recorded spend decision; keep every release surface honest."""
    if not pending(conn, task_id):
        return None
    from hermes_cli.kanban_refusal_receipt import restricted
    if restricted(conn, task_id):
        return 'Provider safety restriction retained; no supported execution release. Owning supervisor must resolve the scope/authority boundary without rerunning refused work.'
    return (f"cannot unblock {task_id}: spend-budget decision pending; split or re-scope the work, "
            f"then use `hermes kanban promote {task_id} --reason <what changed>`, or archive it")


def notice(payload: dict) -> str:
    if payload.get('cause') == 'safety_refusal':
        return ('Provider safety refusal retained. There is no supported execution release; '
                'ordinary promotion, review, comments and timers are not clearance. '
                'The owning supervisor may resolve the scope/authority boundary or withdraw '
                'the work without declaring incomplete acceptance satisfied. Do not rerun the refused work.')
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
    from hermes_cli.kanban_refusal_receipt import restricted
    safety = restricted(conn, task_id)
    guidance = notice({'reason': note, **({'cause': 'safety_refusal'} if safety else {})})
    action = ('\n\nRecord the administrative scope/authority decision without execution release. '
              'Do not run the original work here.' if safety else
              f"\n\nRecord the decision on {task_id}; use `hermes kanban promote {task_id} --reason '<what changed>'` "
              'only after re-scoping or justifying further spend, or archive it. Do not run the original work here.')
    sup = kb.create_task(
        conn, title=forwarded_title(f"Spend budget decision: {task.title}"[:200], f"Spend budget decision: {task_id}"),
        body=f"Card {task_id} has stopped. No chat owns its decision.\n\n"
             + "Source title and stop reason (historical evidence):\n" + forwarded_text(task.title) + "\n"
             + forwarded_text(note) + "\n\n" + guidance + action,
        assignee=_supervisor_profile(), created_by="kanban-spend-budget", workspace_kind="scratch",
        idempotency_key=f"spend-budget-decision:{task_id}:{episode}", tenant=task.tenant)
    return {"kind": "supervisor", "task_id": sup}
