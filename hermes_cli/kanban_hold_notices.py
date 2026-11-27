"""Every-tick decision delivery, independent of stale triage and spawn capacity. [daveey]

Only the card home produces events. Foreign owning chats consume the existing
replica envelope; a notification never renews scope or satisfies acceptance.
"""
from __future__ import annotations


def destinations(conn, task_id: str):
    """Actual authority addresses, shared by binding and idle missing-link detection."""
    from hermes_cli.kanban_hold_audience import describe
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name
    own, profile = gateway_names()[0], get_active_profile_name()
    for link in describe(conn, task_id)['chats']:
        gw, prof, session = link['gateway'], link['profile'], link['session']
        local = gw == own and prof == profile
        yield 'tui' if local else 'hub', session if local else f'{gw}/{prof}/{session}'


def bind(conn, task_id: str, *, replay_event: int | None = None) -> None:
    """Bind before publication, or replay an existing notice only for a new recipient."""
    for platform, chat in destinations(conn, task_id):
        conn.execute("INSERT OR IGNORE INTO kanban_notify_subs "
                     "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
                     "VALUES (?,?,?,'','notify','{\"reason\":\"origin\"}',unixepoch(),"
                     "COALESCE(?,(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?)))",
                     (task_id, platform, chat, replay_event - 1 if replay_event else None, task_id))


def creator_pending(conn, task_id: str, identity: str) -> bool:
    """Read-only destination/dedup preflight; callers recheck under their writer."""
    from hermes_cli.kanban_archive_scope import _creator_task_id
    from hermes_cli.kanban_home_transfer import frozen
    from hermes_cli.kanban_wake_check import foreign_ids
    owner = _creator_task_id(conn, task_id)
    # A frozen creator is deferred, not satisfied: no dedup event is written, so
    # sweeps re-offer the handoff once custody is released or aborted.
    return bool(owner and owner not in foreign_ids(conn) and not frozen(conn, owner) and conn.execute(
        'SELECT 1 FROM tasks WHERE id=?', (owner,)).fetchone() and not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='hold_creator_notified' "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.identity') END=? "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.source_task_id') END=?",
        (owner, identity, task_id)).fetchone())


def creator(conn, task_id: str, identity: str, body: str) -> None:
    """Same-transaction handoff to a recorded creator; no invented guard/decider card."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_archive_scope import _creator_task_id
    if not creator_pending(conn, task_id, identity):
        return
    owner = _creator_task_id(conn, task_id)
    kb.add_comment(conn, owner, 'kanban-decision-notice', f'{task_id}: {body}', system=True)
    kb._append_event(conn, owner, 'hold_creator_notified',
                     {'identity': identity, 'source_task_id': task_id, 'creator_task_id': owner})




def publish(conn, task_id: str, *, with_producer: bool = False) -> None:
    """Publish one guidance revision; bind authentic recipients before its event cursor."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_hold_effective import current, previous
    hold = current(conn, task_id)
    if hold is None:
        return
    payload = hold[1]
    prior = previous(conn, task_id, payload)
    bind(conn, task_id, replay_event=prior['id'] if prior else None)
    prior_prod = kb._json_dict(prior['payload']).get('producer') if prior else None
    episode = payload.get('episode')
    historical = decision.episode_route(conn, task_id, episode) if with_producer and isinstance(episode, str) else None
    if historical is not None:
        # Reuse a legacy wrapper, including a same-episode dismissal, without minting a guard.
        if historical != prior_prod:
            kb._append_event(conn, task_id, decision.NOTICE, {**payload, 'producer': historical})
    elif prior is None:
        if with_producer:
            payload = {**payload, 'producer': decision.producer(conn, task_id, payload.get('reason') or 'Hold')}
        kb._append_event(conn, task_id, decision.NOTICE, payload)
    creator(conn, task_id, payload['revision'], decision.creator_notice(payload))


def sweep(conn) -> None:
    """Repair fresh canonical retains in one dispatch tick, not hours later in the age sweep."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_dispatch_storage import DispatchWriteContended
    from hermes_cli.kanban_hold_effective import current, previous
    from hermes_cli.kanban_spend_decision import strict_episode
    from hermes_cli.kanban_wake_check import foreign_ids
    from hermes_cli.kanban_home_transfer import frozen
    foreign = foreign_ids(conn)
    for (tid,) in conn.execute("SELECT id FROM tasks WHERE status='triage'").fetchall():
        if tid in foreign or frozen(conn, tid):
            continue
        try:
            # A malformed historical cause publishes no notice: the strict read raises
            # here and this card's hold is retained while siblings and dispatch go on.
            strict_episode(conn, tid)
            hold = current(conn, tid)
            if hold is None:
                continue
            # New recipients/restrictions are revisions even when the base episode
            # was delivered. Unchanged polls remain read-only.
            if previous(conn, tid, hold[1]) is not None and not creator_pending(
                    conn, tid, hold[1]['revision']) and all(conn.execute(
                    'SELECT 1 FROM kanban_notify_subs WHERE task_id=? AND platform=? AND chat_id=?',
                    (tid, platform, chat)).fetchone() for platform, chat in destinations(conn, tid)):
                continue
            with kb.write_txn(conn):
                row = conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()
                if row is not None and row[0] == 'triage' and not frozen(conn, tid):
                    publish(conn, tid)
        except DispatchWriteContended:
            raise  # Stop the pass, rather than retrying every hold against the same busy writer.
        except Exception:  # health: allow BLE001 -- rollback this publication, retain its hold and retry next tick
            kb._log.warning('kanban: hold notice failed for %s; hold retained', tid, exc_info=True)
