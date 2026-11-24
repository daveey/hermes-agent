"""Durable decision hold and producer for a cumulative spend stop. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_db as kb


EVENT = "spend_budget_triaged"
NOTICE = "spend_budget_decision_ready"


def ensure_producer(conn, task_id: str) -> None:
    """Repair pre-notice stops too; one durable producer receipt per spend-stop episode."""
    # Most sweeps are no-ops. Do not queue for the global SQLite writer just
    # to discover a live route: this runs before ready-card admission while the
    # dispatcher holds its tick lock. Every actual repair is re-read below.
    if _producer_repair(conn, task_id) is None:
        return
    with kb.write_txn(conn, allow_nested=True):
        repair = _producer_repair(conn, task_id)
        if repair is None:
            return
        episode, payload, route, live = repair
        from hermes_cli import kanban_spend_reviewer as reviewer
        if not live and reviewer.queue(conn, task_id, episode):
            return  # an immediate review decides first; it routes the stop here only if it escalates
        if not live:
            route = producer(conn, task_id, payload.get('reason') or 'Spend budget reached')
        kb._append_event(conn, task_id, NOTICE, {**payload, "episode": episode, "producer": route})


def _producer_repair(conn, task_id):
    """Read-only need check; never authorizes a repair outside its transaction."""
    import json
    stop = pending_episode(conn, task_id)
    if stop is None:
        return None
    episode = stop.get("uid") or str(stop["id"])
    from hermes_cli import kanban_spend_reviewer as reviewer
    if reviewer.holds(conn, task_id, episode):
        return None
    previous = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? "
                            "AND json_extract(payload,'$.episode')=? ORDER BY id DESC LIMIT 1",
                            (task_id, NOTICE, episode)).fetchone()
    payload = json.loads(stop["payload"] or "{}")
    from hermes_cli.kanban_refusal_receipt import restricted
    safety = restricted(conn, task_id)
    scope = execution_restricted(conn, task_id) and not safety
    prior = json.loads(previous[0]) if previous else None
    cause = 'safety_refusal' if safety else 'scope_stopped' if scope else None
    prior_route = prior.get('producer') if isinstance(prior, dict) else None
    route = episode_route(conn, task_id, episode) or prior_route
    live = _live_route(conn, route)
    if live and route == prior_route and (cause is None or prior.get('cause') == cause):
        return None
    if cause:
        payload['cause'] = cause
    return episode, payload, route, live



def episode_route(conn, task_id: str, episode: str) -> dict | None:
    """Reuse historical wrappers too; a closed same-episode decision is a disposition, not release."""
    row = conn.execute(
        "SELECT t.id,t.status FROM tasks t WHERE t.created_by='kanban-spend-budget' AND "
        "(t.idempotency_key=? OR EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.id "
        "AND e.kind='administrative_decision' AND json_valid(e.payload) "
        "AND json_extract(e.payload,'$.root_task')=? AND json_extract(e.payload,'$.root_episode')=?)) "
        "ORDER BY CASE WHEN t.status IN ('done','archived') THEN 0 ELSE 1 END,t.created_at,t.id LIMIT 1",
        (f"spend-budget-decision:{task_id}:{episode}", task_id, episode)).fetchone()
    return ({'kind': 'dismissed' if row['status'] in ('done', 'archived') else 'supervisor',
             'task_id': row['id']} if row else None)


def dismissed(conn, task_id: str) -> bool:
    """The stranded backstop must not re-ask a decision the desk already closed."""
    import json
    stop = pending_episode(conn, task_id)
    if stop is None:
        return False
    episode = stop.get('uid') or str(stop['id'])
    route = episode_route(conn, task_id, episode)
    if route is None:
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
                           "AND json_extract(payload,'$.episode')=? ORDER BY id DESC LIMIT 1",
                           (task_id, NOTICE, episode)).fetchone()
        route = json.loads(row[0]).get('producer') if row else None
    return bool(route and route['kind'] == 'dismissed')


def _live_route(conn, route) -> bool:
    """Live ownership or a terminal same-episode disposition satisfies routing, never execution."""
    if not isinstance(route, dict):
        return False
    if route.get('kind') == 'supervisor':
        task = kb.get_task(conn, route.get('task_id'))
        return task is not None and task.status not in ('done', 'archived')
    # Explicit administrative exhaustion is a terminal routing boundary, not
    # permission to recurse into another decision owner or release the root hold.
    return route.get('kind') in ('chats', 'unavailable', 'dismissed')


def repair_triage_routes(conn, *, skip: set[str]) -> None:
    """Repair ownership independently of idle/release eligibility; never change task status."""
    rows = conn.execute("SELECT t.id FROM tasks t WHERE t.status='triage' AND EXISTS "
                        "(SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.kind=?)", (EVENT,)).fetchall()
    for row in rows:
        if row[0] not in skip:
            safe_ensure_producer(conn, row[0])


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
        "('spend_budget_triaged','decision_hold_restricted','decision_retained','promoted_manual','archived')", (task_id,))]
    events.sort(key=_event_order, reverse=True)
    receipts = {e['id'] for e in receipt_restrictions(conn, task_id)}
    from hermes_cli import kanban_scope_renewal as renewal
    renewed = renewal.covered(conn, task_id)  # scope evidence only; safety is never skipped
    for event in events:
        try:
            payload = json.loads(event['payload'] or '{}')
        except (TypeError, ValueError):
            continue
        if (renewal.key(event) in renewed and isinstance(payload, dict) and (
                payload.get('superseded_by_safety_stop') is not True if event['id'] in receipts
                else payload.get('cause') == 'scope_stopped')):
            continue
        if event['id'] in receipts:
            return next((e for e in events if e['kind'] == EVENT and (e.get('uid') or str(e['id'])) == payload.get('episode_uid')), event)
        if (isinstance(payload, dict) and payload.get('cause') in {'safety_refusal', 'scope_stopped'}
                and event['kind'] in (EVENT, 'decision_hold_restricted')):
            return next((e for e in events if e['kind'] == EVENT
                         and (e.get('uid') or str(e['id'])) == payload.get('episode')),
                        event) if event['kind'] != EVENT else event
    ordinary = [e for e in events if e['kind'] not in ('decision_hold_restricted', 'decision_retained')]
    if ordinary and ordinary[0]['kind'] == EVENT:
        return ordinary[0]
    return renewal.reopened(conn, task_id, ordinary) if renewed else None


def pending(conn, task_id: str) -> bool:
    """Budget promotion/archive never clear an attributable safety restriction."""
    return pending_episode(conn, task_id) is not None


def execution_restricted(conn, task_id: str) -> bool:
    return restricted_since(conn, task_id) is not None


def restricted_since(conn, task_id: str) -> int | None:
    """Execution fencing: the earliest restriction a scope renewal does not cover. Safety is never covered."""
    import json
    from hermes_cli import kanban_scope_renewal as renewal
    renewed = renewal.covered(conn, task_id)
    if not renewed:
        return restricted_floor(conn, task_id)
    times = [e['created_at'] or 0 for e in receipt_restrictions(conn, task_id)
             if renewal.key(e) not in renewed or json.loads(e['payload']).get('superseded_by_safety_stop') is True]
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN "
                            "('spend_budget_triaged','decision_hold_restricted') AND json_valid(payload) "
                            "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.cause') END "
                            "IN ('safety_refusal','scope_stopped')", (task_id,)):
        event = dict(row)
        if json.loads(event['payload'])['cause'] == 'safety_refusal' or renewal.key(event) not in renewed:
            times.append(event['created_at'] or 0)
    return min(times) if times else None


def restricted_floor(conn, task_id: str) -> int | None:
    """Monotone historical floor for spend accounting: a scope renewal never moves it."""
    row = conn.execute("SELECT MIN(COALESCE(created_at,0)) FROM task_events WHERE task_id=? AND kind IN "
                        "('spend_budget_triaged','decision_hold_restricted') AND json_valid(payload) "
                        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.cause') END "
                        "IN ('safety_refusal','scope_stopped')", (task_id,)).fetchone()
    times = [e['created_at'] or 0 for e in receipt_restrictions(conn, task_id)]
    if row[0] is not None:
        times.append(row[0])
    return min(times) if times else None


def receipt_restrictions(conn, task_id: str, *, safety_only: bool = False) -> list[dict]:
    """Only locally independently confirmed receipts bridge a lagging restriction feed.

    Historical reserved-kind lookalikes are not retroactively attested. Foreign
    pickup relies on canonical STOP/RESTRICT rows, not locally invented custody.
    """
    import json
    from hermes_cli.kanban_retain_transport import command, confirmed_command
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_failure_commands'").fetchone():
        return []
    out = []
    for row in conn.execute("SELECT e.*,r.uid AS bound_run_uid FROM task_events e LEFT JOIN task_runs r ON r.id=e.run_id "
                            "WHERE e.task_id=? AND e.kind='decision_retained'", (task_id,)):
        event = dict(row)
        run_uid = event['bound_run_uid']
        owned = command(conn, run_uid)
        if not owned or not confirmed_command(conn, task_id, run_uid) or owned.get('operation_kind') != 'retain':
            continue
        receipt = json.loads(owned['canonical_receipt_json'])
        if event.get('uid') != owned['event_uid'] or json.loads(event['payload']) != receipt:
            continue
        safety = receipt.get('superseded_by_safety_stop') is True
        if safety or not safety_only and receipt.get('disposition', {}).get('reason') == 'scope_stopped':
            out.append(event)
    return out


def execution_refusal(conn, task_id: str) -> str | None:
    """Cause-specific diagnostic only; never converts a scope stop into clearance."""
    from hermes_cli.kanban_refusal_receipt import restricted
    if restricted(conn, task_id):
        return 'Provider safety restriction retained; no supported execution release. A budget decision is not clearance. Owning supervisor must resolve the scope/authority boundary without rerunning refused work.'
    if execution_restricted(conn, task_id):
        from hermes_cli.kanban_scope_renewal import ROUTE
        return ('Scope stopped; promotion, comments, timers and review do not release it. To renew the scope: '
                + ROUTE.format(task=task_id) + '.')
    return None


def unblock_refusal(conn, task_id: str) -> str | None:
    """An unblock is not a recorded spend decision; keep every release surface honest."""
    if not pending(conn, task_id):
        return None
    if refusal := execution_refusal(conn, task_id):
        return refusal
    return (f"cannot unblock {task_id}: spend-budget decision pending; split or re-scope the work, "
            f"then use `hermes kanban promote {task_id} --reason <what changed>`, or archive it")


def notice(payload: dict) -> str:
    if payload.get('cause') == 'scope_stopped':
        from hermes_cli.kanban_scope_renewal import ROUTE
        return ('Scope stopped without execution authority. No generic promotion, comment, timer or review releases it. '
                'Only an explicit scope renewal does: ' + ROUTE.format(task=payload.get('task_id') or '<task>') + '.')
    if payload.get('cause') == 'safety_refusal':
        retried = payload.get('refused_providers')
        head = ('Provider safety refusal retained' if not retried else
                'Provider safety refusal retained after the work was retried unchanged on other eligible providers '
                f'until every one refused ({", ".join(str(p) for p in retried)})')
        return (head + '. There is no supported execution release; '
                'ordinary promotion, review, comments and timers are not clearance. '
                'The owning supervisor may resolve the scope/authority boundary or withdraw '
                'the work without declaring incomplete acceptance satisfied. Do not rerun the refused work.')
    return (str(payload.get("reason") or "Spend budget reached") + ". Held in triage until a recorded decision: "
            "split or re-scope the work, then manually promote it, or archive it. "
            "Do not requeue unchanged; the stale-triage sweep cannot release this hold.")


def producer(conn, task_id: str, note: str) -> dict:
    """Inside the stop transaction: one chat-notice route OR one supervisor, never both."""
    from hermes_cli.profiles import get_active_profile_name
    import json
    role = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='administrative_decision' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if role:
        root = json.loads(role[0])
        return {'kind': 'unavailable', 'root_task': root['root_task'], 'root_episode': root['root_episode'],
                'error': 'administrative_owner_exhausted'}

    def bind(platform, chat_id):
        conn.execute(
            "INSERT OR IGNORE INTO kanban_notify_subs "
            "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
            "VALUES (?,?,?,'','notify','{\"reason\":\"origin\"}',unixepoch(),"
            "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?))",
            (task_id, platform, chat_id, task_id))
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_stranded_park import Liveness, local_chat_dead, settings

    task = kb.get_task(conn, task_id)
    assert task is not None
    own = gateway_names()[0]
    active = get_active_profile_name()
    with Liveness(idle=settings()[1]) as liveness:
        return _route(conn, task_id, task, own, active, bind, note,
                      lambda gw, profile, session: local_chat_dead(gw, profile, session, own=own, liveness=liveness))


def _route(conn, task_id, task, own, active, bind, note, dead) -> dict:
    """``producer`` body after setup. ``dead``: a chat of this gateway that is archived/hidden/missing, or ended and
    idle, is no route for the decision: the notice would sit unread (kanban_stranded_park). One ``Liveness`` per
    call: each state.db is opened at most once."""
    from hermes_cli.kanban_model_neutral import forwarded_text, forwarded_title
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_db_notify import is_headless_session
    from hermes_cli.kanban_triage_sweep import _supervisor_profile

    # Bind legacy origin sessions before appending the notice so its cursor cannot skip this event.
    if task.session_id and not is_headless_session(task.session_id) and not dead(own, active, task.session_id):
        bind("tui", task.session_id)
    links = (_chat_links(conn, task_id) if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone() else [])
    for link in links:
        session = link["chat_session"]
        gw, profile = link["chat_gateway"], link["chat_profile"] or "default"
        chat_id = f"{gw}/{profile}/{session}"
        if not is_headless_session(chat_id) and not dead(gw, link["chat_profile"], session):
            local = gw == own and profile == active
            bind("tui" if local else "hub", session if local else chat_id)
    from hermes_cli.kanban_db_notify import _owning_sub_clause
    from hermes_cli.kanban_origin import OriginError, parse_origin
    for sub in conn.execute("SELECT s.platform, s.chat_id, s.notifier_profile FROM kanban_notify_subs s "
                            f"JOIN tasks p ON p.id=s.task_id WHERE s.task_id=? AND {_owning_sub_clause()}",
                            (task_id,)).fetchall():
        platform, chat_id, notifier = sub[0], sub[1], sub[2]
        if platform == "tui" and dead(own, notifier, chat_id):
            continue
        if platform == "hub":
            try:
                origin = parse_origin(chat_id)
            except OriginError:
                origin = None
            if origin is not None and dead(origin.gateway, origin.profile, origin.session):
                continue
        return {"kind": "chats"}
    stop = pending_episode(conn, task_id)
    episode = (stop.get('uid') or str(stop['id'])) if stop else None
    existing = episode_route(conn, task_id, episode) if episode else None
    if existing:
        return existing
    from hermes_cli.kanban_refusal_receipt import restricted
    safety = restricted(conn, task_id)
    stopped = execution_restricted(conn, task_id)
    import json as _json
    stop = pending_episode(conn, task_id)
    listed = _json.loads(stop['payload'] or '{}').get('refused_providers') if stop is not None and safety else None
    guidance = notice({'reason': note, **({'cause': 'safety_refusal' if safety else 'scope_stopped'} if stopped else {}),
                       **({'refused_providers': listed} if listed else {})})
    action = ('\n\nRecord the administrative scope/authority decision without execution release. '
              'Do not run the original work here. Administrative acceptance is decision recorded; authority unavailable, not root-work completion.' if stopped else
              f"\n\nRecord the decision on {task_id}; use `hermes kanban promote {task_id} --reason '<what changed>'` "
              'only after re-scoping or justifying further spend, or archive it. Do not run the original work here.')
    sup = kb.create_task(
        conn, title=forwarded_title(f"Spend budget decision: {task.title}"[:200], f"Spend budget decision: {task_id}"),
        body=f"Card {task_id} has stopped. No chat owns its decision.\n\n"
             + "Source title and stop reason (historical evidence):\n" + forwarded_text(task.title) + "\n"
             + forwarded_text(note) + "\n\n" + guidance + action
             + "\n\nApply the disposition on the source before completing this decision card; a recommendation or "
             "completion alone does not change its hold. If authority/capability is unavailable, retain this "
             "decision card as a non-executing owner with the exact prerequisite instead of completing and "
             "leaving the source ownerless. Never renew spend or bypass safety/scope gates implicitly.",
        assignee=_supervisor_profile(), created_by="kanban-spend-budget", workspace_kind="scratch",
        idempotency_key=f"spend-budget-decision:{task_id}:{episode}", tenant=task.tenant)
    stop = pending_episode(conn, task_id)
    kb._append_event(conn, sup, 'administrative_decision', {'role': 'administrative_decision',
                     'root_task': task_id, 'root_episode': (stop.get('uid') or str(stop['id'])) if stop else None})
    return {"kind": "supervisor", "task_id": sup}
