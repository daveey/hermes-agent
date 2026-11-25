"""Durable decision hold and producer for a cumulative spend stop. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_db as kb

EVENT = "spend_budget_triaged"
NOTICE = "spend_budget_decision_ready"


def ensure_producer(conn, task_id: str) -> None:
    """Repair pre-notice stops too; one durable producer receipt per spend-stop episode."""
    from hermes_cli.kanban_hold_notices import publish
    # Most sweeps are no-ops. Do not queue for the global SQLite writer just
    # to discover a live route: this runs before ready-card admission while the
    # dispatcher holds its tick lock. Every actual repair is re-read below.
    if not _notice_repair(conn, task_id):
        return
    with kb.write_txn(conn, allow_nested=True):
        if not _notice_repair(conn, task_id):
            return
        # Ordinary spend review stays eligible, but it is not a substitute for
        # immediate owner visibility. A queued judge grants no execution here.
        from hermes_cli import kanban_spend_reviewer as reviewer
        stop = pending_episode(conn, task_id)
        if stop is not None:
            reviewer.queue(conn, task_id, stop.get('uid') or str(stop['id']))
        publish(conn, task_id, with_producer=True)


def _notice_repair(conn, task_id: str) -> bool:
    """Read-only need check; never authorizes a repair outside its transaction."""
    from hermes_cli.kanban_hold_effective import current, previous
    from hermes_cli.kanban_hold_notices import destinations
    hold = current(conn, task_id)
    if hold is None:
        return False
    payload = hold[1]
    prior = previous(conn, task_id, payload)
    prior_prod = kb._json_dict(prior['payload']).get('producer') if prior else None
    episode = payload.get('episode')
    historical = episode_route(conn, task_id, episode) if isinstance(episode, str) else None
    if historical is not None:
        if historical != prior_prod:
            return True
    elif prior is None:
        return True
    if any(not conn.execute(
            'SELECT 1 FROM kanban_notify_subs WHERE task_id=? AND platform=? AND chat_id=?',
            (task_id, platform, chat)).fetchone() for platform, chat in destinations(conn, task_id)):
        return True
    from hermes_cli.kanban_archive_scope import _creator_task_id
    from hermes_cli.kanban_wake_check import foreign_ids
    owner = _creator_task_id(conn, task_id)
    if not owner or owner in foreign_ids(conn) or not conn.execute(
            'SELECT 1 FROM tasks WHERE id=?', (owner,)).fetchone():
        return False
    return not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='hold_creator_notified' "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.identity') END=? "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.source_task_id') END=?",
        (owner, payload['revision'], task_id)).fetchone()



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
    # A bare ``unavailable`` (a stop with a cause and no live chat) holds only until
    # a chat of the card is live again: ``_producer_repair`` re-checks it.
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
        # A malformed cause cannot be interpreted as an ordinary budget stop:
        # callers must retain the hold rather than infer permission to release.
        if isinstance(payload, dict) and payload.get('cause') is not None and not isinstance(payload['cause'], str):
            raise ValueError('Malformed spend stop cause')
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
    """Execution fencing: the earliest restriction a scope renewal does not cover. Safety is never covered.
    An owning-chat administrative stop (``kanban_stop_ended``) is never covered either, and never moves the
    accounting floor."""
    from hermes_cli.kanban_stop_ended import stopped_at
    admin = stopped_at(conn, task_id)
    since = _restricted_since(conn, task_id)
    return min(t for t in (admin, since) if t is not None) if (admin, since) != (None, None) else None


def _restricted_since(conn, task_id: str) -> int | None:
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
                + ROUTE.format(task=task_id) + '. ' + _one_step(task_id) + '.')
    return None


def unblock_refusal(conn, task_id: str) -> str | None:
    """An unblock is not a recorded spend decision; keep every release surface honest."""
    if not pending(conn, task_id):
        return None
    if refusal := execution_refusal(conn, task_id):
        return refusal
    return (f"cannot unblock {task_id}: spend-budget decision pending; split or re-scope the work, "
            f"then use `hermes kanban promote {task_id} --reason <what changed>`, or archive it. "
            + _one_step(task_id))


def _one_step(task_id, *, for_worker=False) -> str:
    """``for_worker``: text for a decision card's own worker, which is fenced from the verb."""
    from hermes_cli.kanban_release import ONE_STEP, WORKER_NOTE
    return (WORKER_NOTE if for_worker else ONE_STEP).format(task=task_id or '<task>')


CAUSED_STOPS = frozenset({'scope_stopped', 'capability_unavailable', 'safety_refusal'})


def caused(payload) -> str | None:
    """The cause of a stop the worker made itself; ordinary budget stops are None."""
    cause = payload.get('cause') if isinstance(payload, dict) else None
    return cause if isinstance(cause, str) and cause in CAUSED_STOPS else None


def caused_label(cause: str) -> str:
    """Caused stops are not budget exhaustion: 'stopped itself: <cause>; awaiting a decision'."""
    from agent.i18n import t
    return t('gateway.kanban.wake.caused_stop_decision_ready', cause=cause)


def wake_status(payloads) -> str:
    """Wake status for the newest decision notice in a batch."""
    from agent.i18n import t
    cause = caused(([p for p in payloads if isinstance(p, dict)] or [{}])[-1])
    return caused_label(cause) if cause else t(f'gateway.kanban.wake.{NOTICE}')


def notice(payload: dict) -> str:
    if cause := caused(payload):
        return caused_label(cause) + '. ' + _detail(payload)
    return _detail(payload)


def creator_notice(payload: dict) -> str:
    """A recorded creator also reads the stop's own reason; a caused stop's label still leads."""
    reason, cause = payload.get('reason'), caused(payload)
    if not cause:
        return str(reason or 'Hold') + '. ' + notice(payload)
    if not reason or cause == 'capability_unavailable':  # a capability detail already carries its reason
        return notice(payload)
    return caused_label(cause) + '. ' + str(reason) + '. ' + _detail(payload)


def _detail(payload: dict) -> str:
    if payload.get('cause') == 'scope_stopped':
        from hermes_cli.kanban_scope_renewal import ROUTE
        from hermes_cli.kanban_hold_audience import chats, guidance
        authority = payload.get('authority')
        prefix = guidance(authority) + ' '
        if not chats(authority) and not (isinstance(authority, dict) and authority.get('creator_active') is True):
            return prefix + 'Promotion, comments, timers and review do not release this scope stop.'
        return ('Scope stopped without execution authority. No generic promotion, comment, timer or review releases it. '
                + prefix + 'Only an explicit scope renewal does: ' + ROUTE.format(task=payload.get('task_id') or '<task>') + '. '
                + _one_step(payload.get('task_id')) + '.')
    if payload.get('cause') == 'safety_refusal':
        retried = payload.get('refused_providers')
        retried = retried if isinstance(retried, (list, tuple)) else []
        head = ('Provider safety refusal retained' if not retried else
                'Provider safety refusal retained after the work was retried unchanged on other eligible providers '
                f'until every one refused ({", ".join(str(p) for p in retried)})')
        return (head + '. There is no supported execution release; '
                'ordinary promotion, review, comments and timers are not clearance. '
                'The owning supervisor may resolve the scope/authority boundary or withdraw '
                'the work without declaring incomplete acceptance satisfied. Do not rerun the refused work.')
    # A capability stop's reason is the worker's own summary; never default it to a budget claim.
    reason = payload.get("reason") or (None if caused(payload) else "Spend budget reached")
    return ((str(reason) + ". " if reason else "") + "Held in triage until a recorded decision: "
            "split or re-scope the work, then manually promote it, or archive it. "
            "Do not requeue unchanged; the stale-triage sweep cannot release this hold. "
            + _one_step(payload.get('task_id'), for_worker=bool(payload.get('for_worker'))) + '.')


def producer(conn, task_id: str, note: str) -> dict:
    """Inside the stop transaction: bind authentic owners without creating guard cards."""
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
    return _route(conn, task_id, gateway_names()[0], get_active_profile_name(), bind)


def _route(conn, task_id, own, active, bind) -> dict:
    """Route only to genuine renewable authority, independent of chat idleness."""
    from hermes_cli.kanban_hold_audience import describe
    authority = describe(conn, task_id)
    # Idleness is not a release of authentic ownership. Do not manufacture a
    # supervisor card: it cannot repair missing historical authority either.
    for chat in authority['chats']:
        gateway, profile, session = chat['gateway'], chat['profile'], chat['session']
        local = gateway == own and profile == active
        bind('tui' if local else 'hub', session if local else f'{gateway}/{profile}/{session}')
    if authority['chats']:
        return {'kind': 'chats'}
    if authority['creator_task_id']:
        return {'kind': 'creator', 'task_id': authority['creator_task_id']}
    return {'kind': 'unavailable', 'error': 'scope_authority_unavailable'}
