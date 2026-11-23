"""Detached Kanban inputs for annotation ViewFacts. [daveey]

Run only on an authorized source owner, never a request or pure reducer. Reuse
canonical transaction readers and raw execution observations; do not adopt host
copies or re-date the legacy count memo. Independent boards are a source vector,
not a fictitious cross-database transaction.
"""
from contextlib import closing
import math
import time

from hermes_cli.fact_capture import CaptureBudgetExceeded


def _add(into, values):
    for sid, count in values.items():
        into[sid] = into.get(sid, 0) + count


def _remote(cards, maps, keys, execution):
    from hermes_cli import kanban_session_activity as activity
    active, blocked, _, stale, _, parked, _, stalled = maps
    for card in cards:
        bucket = activity._replica_bucket(card)
        into = active if bucket == 'active' else blocked if bucket == 'blocked' else None
        if into is None:
            continue
        sid = card['session']
        into[sid] = into.get(sid, 0) + 1
        is_parked = bucket == 'active' and card.get('status') in (
            *activity.PARKED_STATUSES, 'todo', *activity.PENDING_RELEASE_STATUSES)
        if is_parked:
            parked[sid] = parked.get(sid, 0) + 1
            keys.setdefault(sid, set()).update(activity._external_activity.replica_markers(card))
        keys.setdefault(sid, set()).add((str(card.get('key')), str(bucket), is_parked))
        activity._replica_exec.collect(card, execution)
        if card.get('status') not in activity.PENDING_RELEASE_STATUSES and (
                activity._remote_wait(card) or {}).get('wait_state') == 'stalled':
            stalled[sid] = stalled.get(sid, 0) + 1
            keys[sid].add((str(card.get('key')), 'stalled', False))
        if card.get('stale'):
            stale[sid] = stale.get(sid, 0) + 1


def _attention(paths, now, due):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_session_activity_attention import _check_passes
    checks = tuple(activity._done_checks.values())
    counts, pairs = {}, set()
    if not checks:
        return counts
    # These are operational plugin captures. Do not use False-on-error legacy
    # verdict caches: a swallowed provider failure cannot certify fresh facts.
    with _check_passes(checks):
        for path in paths:
            for sid, card in activity._read_recent_done(path, now - activity.ATTENTION_WINDOW_S):
                if any(check(card) for check in checks):
                    pairs.add((sid, str(card['id'])))
                completed = card.get('completed_at')
                if isinstance(completed, (int, float)) and math.isfinite(completed):
                    due.append(math.nextafter(math.nextafter(completed, math.inf)
                                              + activity.ATTENTION_WINDOW_S, math.inf))
    for sid, _ in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    return counts


def capture(budget, *, now):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_external_activity as external_activity
    from hermes_cli.fact_contract import freeze
    from hermes_cli.fact_wire import encode
    started = time.monotonic()
    paths = tuple(activity._board_db_paths())
    maps = tuple({} for _ in range(8))
    active, blocked, unseen, _stale, waiting, parked, held, stalled = maps
    local_keys, execution, external = {}, {}, {}
    due = [now + 30]
    total = 0
    # _read_board's historical pair sink is process-wide. Use its existing
    # lifecycle lock, with bounded acquisition, even though our outputs are local.
    if not activity._pairs_lock.acquire(timeout=budget.max_seconds):
        raise CaptureBudgetExceeded('kanban capture lock deadline exceeded')
    try:
        for path in paths:
            reads, complete = activity._read_board(path, parked=True, stalled=True)
            if not complete:
                raise RuntimeError('incomplete native kanban board capture')
            total += len(reads['pairs']) + len(reads.get('done_pairs', ()))
            if total > budget.max_keys or time.monotonic() - started >= budget.max_seconds:
                raise CaptureBudgetExceeded('kanban capture inventory/time budget exceeded')
            for into, name in ((active, 'active'), (blocked, 'blocked'), (unseen, 'unseen')):
                _add(into, reads[name])
            for sid, tid in reads['pairs']:
                waiting.setdefault(sid, set()).add(tid)
                raw = reads.get('execution', {}).get(tid)
                if raw is not None:
                    execution.setdefault(sid, {})[f'{activity._board_slug(path)}/{tid}'] = raw
            for sid, tid in reads.get('done_pairs', ()):
                execution.setdefault(sid, {})[f'done:{activity._board_slug(path)}/{tid}'] = reads['done_execution'][tid]
            for into, values in zip((parked, held), reads['parked']):
                _add(into, values)
            stalled_pairs = set()
            _add(stalled, activity._stalled_counts(*reads['stall_rows'], now=now, keys=stalled_pairs))
            for sid, keys in activity._identity.local_keys(path, {**reads, 'stalled_pairs': stalled_pairs},
                                                         board=activity._board_slug(path)).items():
                local_keys.setdefault(sid, set()).update(keys)
            # A separate canonical external transaction has its own captured
            # observation dates. Errors propagate instead of empty success.
            with closing(activity._open_ro(path)) as conn:
                conn.execute('BEGIN')
                values = external_activity.read_session_external(conn, activity._parked_pairs(conn), now)
            for sid, slot in values.items():
                target = external.setdefault(sid, {'counts': [0, 0, 0], 'cards': []})
                target['counts'] = [a + b for a, b in zip(target['counts'], slot['counts'])]
                target['cards'] += slot['cards']
    finally:
        activity._pairs_lock.release()
    remote = activity._remote_cards(None, activity.LISTED_STATUSES, False)
    if total + len(remote) > budget.max_keys:
        raise CaptureBudgetExceeded('kanban remote inventory exceeds key budget')
    remote_keys = {}
    _remote(remote, maps, remote_keys, execution)
    parts = (*maps[:4], waiting, parked, held, stalled, activity._identity.merge_keys(remote_keys, local_keys),
             activity._extra_waiting(), _attention(paths, now, due), execution)
    _, provider_due = activity._host_copy.waiting_events(now)
    if provider_due is not None:
        due.append(provider_due)
    value = freeze({'parts': parts, 'observed_at': now, 'external': external})
    if len(encode(value)) > budget.max_bytes or time.monotonic() - started >= budget.max_seconds:
        raise CaptureBudgetExceeded('kanban capture byte/time budget exceeded')
    return value, min((d for d in due if d > now), default=now + 30)
