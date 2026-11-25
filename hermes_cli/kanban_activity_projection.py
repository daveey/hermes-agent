"""Canonical detached Kanban annotation projection, shared by legacy and facts.

Inputs are captured by the source owner. No discovery, cache lookup, clock read or
source access belongs here. Rows are caller-owned; the captured maps are read-only.
"""
from hermes_cli import kanban_activity_identity as identity
from hermes_cli import kanban_external_activity as external_activity
from hermes_cli.kanban_execution_observations import count_snapshot
from hermes_cli.kanban_open_asks import stamp_needs_you

WAITING_ON_CAP = 50


def project_kanban_unknown(rows, *, now):
    """Preserve canonical unavailable semantics, including retained history."""
    identity.mark_unknown(rows)
    for row in rows:
        count_snapshot(row, (), now=now)
    return rows


def project_kanban_activity(rows, *, parts, observed_at, now, external=None):
    """Project a detached canonical snapshot; ``external=None`` leaves that family alone.

    ``parts`` is (active, blocked, unseen, remote_stale, waiting, parked, held,
    stalled, card_keys, extra, attention, execution). Counts are per-session maps;
    waiting/extra are session->ids, card_keys session->(qualified key,bucket,parked),
    execution session->key->raw observation. ``external`` is the detached map from
    session_external/stamp_boards, not a callable. Pass {} to clear external fields.
    ``observed_at`` is the captured conservative source date, never reducer time.
    """
    (active, blocked, unseen, remote_stale, waiting, parked, held, stalled,
     remote_keys, extra, attention, execution) = parts
    for row in rows:
        row["kanban_observed_at"] = observed_at
        row.pop("kanban_unknown", None)
        ids = list(identity.row_ids(row))
        identity.timers.stamp(row, {key for sid in ids for key in remote_keys.get(sid, ())})
        dup = identity.lineage_duplicates(remote_keys, ids)
        identity.stamp_blocked_owners(row, remote_keys, ids, WAITING_ON_CAP)
        for key, counts in (("kanban_active", active), ("kanban_blocked", blocked), ("kanban_unseen", unseen),
                            ("kanban_attention", attention), ("kanban_remote_stale", remote_stale)):
            n = sum(counts.get(sid, 0) for sid in ids) - dup.get(key, 0)
            if n:
                row[key] = n
            else:
                row.pop(key, None)
        stamp_needs_you(row, ids, remote_keys)
        open_ids = {tid for sid in ids for tid in waiting.get(sid, ())}
        pending, extra_ids = identity.closeout_extra(open_ids, {t for sid in ids for t in extra.get(sid, ())},
                                                    [remote_keys.get(sid, ()) for sid in ids])
        if pending:
            row["kanban_active"] = int(row.get("kanban_active") or 0) + len(pending)
        n_parked = sum(parked.get(sid, 0) for sid in ids) - dup.get("kanban_parked", 0)
        n_held = sum(held.get(sid, 0) for sid in ids) - dup.get("kanban_held", 0)
        if row.get("kanban_active"):
            row["kanban_parked"] = n_parked
        else:
            row.pop("kanban_parked", None)
        if n_held:
            row["kanban_held"] = n_held
        else:
            row.pop("kanban_held", None)
        n_stalled = sum(stalled.get(sid, 0) for sid in ids) - dup.get("kanban_stalled", 0)
        if n_stalled and row.get("kanban_active"):
            row["kanban_stalled"] = n_stalled
        else:
            row.pop("kanban_stalled", None)
        ids_waiting = sorted(open_ids | extra_ids)
        if ids_waiting:
            row["kanban_waiting_on"] = ids_waiting[:WAITING_ON_CAP]
        else:
            row.pop("kanban_waiting_on", None)
        count_snapshot(row, [key for sid in ids for key in remote_keys.get(sid, ())], now=now,
                       evidence={key: value for sid in ids for key, value in execution.get(sid, {}).items()})
    if external is not None:
        external_activity.stamp_rows(rows, external, identity.row_ids)
    return rows
