"""Prevent old GitHub-quota context from undoing a verified recovery."""
from __future__ import annotations

import uuid


def _winning_checks(conn, bid, now):
    """Historical recovery survives new cohorts, but losing tied results do not."""
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id
    from hermes_cli.kanban_shared_blocker_checks import _check_payloads, _assertion_tokens, _checked, _result_key

    token = _assertion_tokens(conn, now).get(bid)
    winners = {}
    for payload in _check_payloads(conn, now):
        checked = _checked(payload)
        if normalize_blocker_id(payload.get('blocker_id')) != bid or checked > now:
            continue
        if checked not in winners or _result_key(payload, token) > _result_key(winners[checked], token):
            winners[checked] = payload
    return winners


def uid_milliseconds(identity):
    """Portable UUIDv7 event/claim clock; other identities have no precision."""
    try:
        uid = uuid.UUID(identity)
    except (ValueError, AttributeError, TypeError):
        return None
    return uid.int >> 80 if uid.version == 7 else None


def consistent_uid_milliseconds(identity, seconds):
    """UUIDv7 milliseconds only when they fall in the row's own recorded second.

    Sync backfills uid-less rows with a v7 uid stamped at backfill time, so a
    uid whose second disagrees with the row's seconds clock is not event time.
    """
    ms = uid_milliseconds(identity)
    return ms if ms is not None and ms // 1000 == seconds else None


def _uid_before_check(identity, started, payload):
    """UUIDv7 carries claim-initiation milliseconds, not remote commit order.

    Use only consistent, strictly earlier clock evidence. Missing/invalid/tied
    precision retains the legacy fallback; UUID random suffixes never order runs.
    """
    claimed_ms = consistent_uid_milliseconds(identity, started)
    checked_ms = payload.get('checked_at_ms')
    if claimed_ms is None or type(checked_ms) is not int:
        return False
    return started == checked_ms // 1000 and claimed_ms < checked_ms


def fence_stale_quota_assertions(conn, states, asserting, *, now):
    """Keep stale cards releasable, but don't treat their old runs as new evidence.

    Only the GitHub quota family opts into this policy. A run that began before
    recovery cannot reopen it. After a verified clear only a run that started
    after the recovery can report a genuinely new wall: ``run_due_checks`` runs
    the cheap check only while a wall is open, so it never re-opens one.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import _mark_cleared, run_effect
    from hermes_cli.kanban_shared_blocker_checks import run_identity

    st = states.get('github-rest-quota')
    if st is None:
        return
    in_flight = {}
    receipts = _winning_checks(conn, st.blocker_id, now)
    # A delayed assertion changes the cohort, not the historical recovery.
    for checked, payload in receipts.items():
        if payload.get('rc') == 0 and isinstance(payload.get('assertion_id'), str):
            _mark_cleared(st, '', None, checked, via='check')
            active = payload.get('active_runs')
            in_flight[checked] = set(map(str, active)) if isinstance(active, list) else None
    if not st.cleared_at:
        return
    eligible = []
    for row in asserting:
        effect, bid = run_effect(row['outcome'], row['metadata'])
        if effect != 'assert' or bid != st.blocker_id:
            continue
        started = kb._to_epoch(row['started_at']) or 0
        ended = kb._to_epoch(row['ended_at']) or 0
        known = in_flight.get(st.cleared_at, False)
        if known is False:
            admitted = started <= st.cleared_at        # non-check clear: unchanged
        elif known is None:
            admitted = started < st.cleared_at         # older receipt: strict timestamps
        else:
            # Snapshot absence does not prove freshness: a remote claim may not
            # have synced yet. Its portable UUID can carry earlier clock evidence.
            identity = run_identity(conn, row['id'])
            admitted = started < st.cleared_at or (
                started == st.cleared_at and (identity in known or
                    _uid_before_check(identity, started, receipts.get(st.cleared_at, {}))))
        if admitted and st.cleared_at < ended:
            if st.behind.get(row['task_id']) == ended:
                st.behind[row['task_id']] = st.cleared_at
        else:
            # A new claim and assertion can both occur after a check but within
            # its coarse timestamp second. Preserve that ordering in derived state.
            eligible.append(max(ended, st.cleared_at + 1) if not admitted and ended == st.cleared_at else ended)
    st.asserted_at = max(eligible, default=0)
    if st.cleared_at > st.asserted_at and not st.first_cleared_at:
        st.first_cleared_at = st.cleared_at
