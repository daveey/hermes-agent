"""Finish a recorded stop decision without renewing scope or granting budget. [daveey]

Home-only maintenance. Existing restriction readers remain authoritative; a
budget edit is never clearance for scope, safety, containment or a human hold.
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision


def _recorded(conn, tid):
    from hermes_cli.kanban_containment_guard import pending as contained
    from hermes_cli.kanban_continuation import HUMAN_BLOCK_KINDS
    from hermes_cli.kanban_goal_recovery import current_park
    from hermes_cli.kanban_resume_requester import open_recovery_card
    from hermes_cli.kanban_wake_check import foreign_copy

    row = conn.execute('SELECT status,human_request,block_kind,claim_lock,current_run_id FROM tasks WHERE id=?',
                       (tid,)).fetchone()
    if (row is None or row[0] != 'triage' or row[1] or row[2] in HUMAN_BLOCK_KINDS or row[3] or row[4]
            or foreign_copy(conn, tid) or contained(conn, tid) or decision.execution_refusal(conn, tid)
            or current_park(conn, tid) or open_recovery_card(conn, tid)):
        return None
    stop = decision.pending_episode(conn, tid)
    if stop is None or stop['kind'] != decision.EVENT:
        return None
    from hermes_cli.kanban_sync_divergence import _event_order
    episode, after = renewal.key(stop), _event_order(stop)
    # Origin order, not local arrival ids: a synced Board may deliver events out of order.
    records = [r for r in renewal.renewals(conn, tid)
               if r['payload']['episode'] == episode and _event_order(r) > after]
    # Canonical review owns placement as well as renewal. A partial feed must
    # not reinterpret wait_on/archive as release before its edges/status arrive.
    if any(r['payload'].get('authority_kind') == 'spend_reviewer' for r in records):
        return None
    # A canonical renewal's own proven mandate is its authorization, not a countermand.
    mandates = set()
    for r in records:
        note = renewal._comment(conn, tid, str(r['payload'].get('mandate_comment_uid') or ''))
        if note is not None:
            mandates.add(note['id'])
    # Require the budget producer's episode-bound decision/comment written by a
    # human: an agent preparing a budget is not authorization. Lookalikes and
    # overrides predating this stop never count.
    from hermes_cli.kanban_release_comments import _resolve
    comments = conn.execute('SELECT * FROM task_comments WHERE task_id=?', (tid,)).fetchall()
    for r in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_set'", (tid,)):
        event = dict(r)
        p = kb._json_dict(event['payload'])
        ref = p.get('decision_comment')
        if (_event_order(event) > after and p.get('episode') == episode and p.get('writer_kind') == 'human'
                and isinstance(ref, dict) and _resolve(comments, event, ref)):
            records.append(event)
    if not records:
        return None
    recorded = max(records, key=_event_order)
    # Conservative: any human/relayed instruction since the stop (before or after
    # the decision) may countermand it; only a renewal's proven mandate is exempt.
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='commented'", (tid,)):
        event = dict(row)
        p = kb._json_dict(event['payload'])
        if ((p.get('writer_kind') == 'human' or p.get('relayed_human') is not None)
                and _event_order(event) > after):
            # Wire-local integer ids can collide after sync. Resolve the comment
            # through the event's portable provenance before exempting a mandate.
            notes = _resolve(comments, event, p)
            if not notes or any(note['id'] not in mandates for note in notes):
                return None
    # Renewal restores scope, never spend: placement needs cumulative budget left.
    if budget.over(budget.spend(conn, tid), budget.card_limits(conn, tid, budget.limits())):
        return None
    return episode, renewal.key(recorded)


def repair_decided_triage(conn) -> list[str]:
    """Atomically settle already-authorized triage decisions; no writer for no-ops."""
    repaired = []
    rows = conn.execute("SELECT id FROM tasks WHERE status='triage'").fetchall()
    for tid, in rows:
        try:
            observed = _recorded(conn, tid)
            if observed is None:
                continue
            with kb.write_txn(conn):
                if _recorded(conn, tid) != observed:
                    continue
                parents = [pid for pid, status in kb.parent_states(conn, tid) if status not in ('done', 'archived')]
                window = ({'spend_budget': budget.card_limits(conn, tid, budget.limits())._asdict()}
                          if budget.limit_window(conn, tid) == 'next_promote' else {})
                # Same decision boundary as release._decide, with placement in the
                # same transaction so an open dependency cannot leave triage stuck.
                kb._append_event(conn, tid, 'promoted_manual', {
                    'actor': 'kanban-decided-triage', 'reason': 'Finish already recorded stop decision',
                    'from_status': 'triage', 'episode': observed[0], 'decision_event': observed[1],
                    'waiting_on_parents': parents, **window})
                conn.execute("UPDATE tasks SET status=? WHERE id=? AND status='triage'",
                             ('todo' if parents else 'ready', tid))
            repaired.append(tid)
        except Exception:  # health: allow BLE001 -- per-card maintenance boundary; rollback retains hold and siblings proceed
            import logging
            logging.getLogger(__name__).exception('decided triage repair failed for %s; hold retained', tid)
    return repaired
