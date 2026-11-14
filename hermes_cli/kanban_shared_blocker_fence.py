"""Prevent old GitHub-quota context from undoing a verified recovery."""
from __future__ import annotations


def fence_stale_quota_assertions(conn, states, asserting, *, now):
    """Keep stale cards releasable, but don't treat their old runs as new evidence.

    Only the GitHub quota family opts into this policy. A run that began before
    recovery cannot reopen it; the dispatcher can discover a genuinely new wall
    on its next cheap check or a newly started worker can report it.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import _LOOKBACK_SECONDS, _mark_cleared, run_effect
    from hermes_cli.kanban_shared_blocker_checks import CHECK_EVENT, run_identity

    st = states.get('github-rest-quota')
    if st is None:
        return
    in_flight = {}
    # Keep successful check evidence even when a delayed assertion supersedes
    # its race token. Token validation at write time already fenced concurrent
    # assertions. Legacy integer-token events are deliberately not accepted.
    for row in conn.execute(
            'SELECT payload FROM task_events WHERE kind=? AND created_at>=?',
            (CHECK_EVENT, now - _LOOKBACK_SECONDS)):
        payload = kb._json_dict(row['payload'])
        if (payload.get('blocker_id') == st.blocker_id and payload.get('rc') == 0
                and isinstance(payload.get('assertion_id'), str)):
            checked = kb._to_epoch(payload.get('checked_at')) or 0
            if checked <= now:
                _mark_cleared(st, '', None, checked, via='check')
                active = payload.get('active_runs')
                if isinstance(active, list):
                    in_flight.setdefault(checked, set()).update(map(str, active))
                else:
                    in_flight[checked] = None   # older receipt: strict timestamp fallback
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
            # Same-second claims are stale only if the receipt saw them running.
            admitted = started < st.cleared_at or (
                started == st.cleared_at and run_identity(conn, row['id']) in known)
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
