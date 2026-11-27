"""Conservative expiry of an absent remote ref's abandoned delivery claim."""
from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path


def _key(ref):
    if not isinstance(ref, str):
        return None
    parts = ref.split('/')
    if len(parts) != 4 or not all(parts) or not re.fullmatch(r't_[A-Za-z0-9_]+', parts[-1]):
        return None
    return '/'.join(parts[1:])


def _expired_observation(conn, row, source, now, remote):
    """Prove every alias in moved_claim's broader identity is expired and in scope."""
    key = _key(row['ref'])
    if key is None:
        return None
    task_id = key.rsplit('/', 1)[-1]
    suffix = '/' + task_id
    observations = conn.execute('SELECT source,ref,first_seen FROM observations WHERE state=? '
                                'AND substr(ref,-length(?))=?',
                                (row['state'], suffix, suffix)).fetchall()
    exact = False
    latest = None
    observed_refs = set()
    for observation in observations:
        seen = observation['first_seen']
        if observation['source'] != source or _key(observation['ref']) != key:
            return None
        if type(seen) not in (int, float) or not math.isfinite(seen):
            return None
        if remote._late_verdict(now, seen) != 'expired':
            return None
        exact |= observation['ref'] == row['ref']
        observed_refs.add(observation['ref'])
        latest = seen if latest is None else max(latest, seen)
    if not exact:
        return None
    aliases = conn.execute('SELECT ref,holder_pid,holder_start FROM notices WHERE session=? AND state=? '
                           'AND substr(ref,-length(?))=?',
                           (row['session'], row['state'], suffix, suffix)).fetchall()
    for alias in aliases:
        if _key(alias['ref']) != key or alias['ref'] not in observed_refs:
            return None
        if remote._holder_is_me(alias['holder_pid']) or remote._holder_alive(alias['holder_pid'], alias['holder_start']):
            return None
    return latest


def settle_absent(conn, selected, states, source, now):
    """One guarded settlement; no source I/O, waits or ambient lookups inside the transaction.

    Observations are insert-only. A future same-source/key alias inherits their
    expired clock (or unknown), rather than acquiring a new delivery window.
    Existing incompatible scopes veto settlement; arbitrary later ledger reset or
    corruption is outside this proof. No mutation is committed without swept.
    """
    from tui_gateway import kanban_remote_notices as remote

    committed = False
    try:
        source = str(Path(source).resolve())  # observation producers use canonical replica identity
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT session,ref,state,holder_pid,holder_start,notice_key,delivered FROM notices '
                           'WHERE session=? AND ref=? AND state=?',
                           (selected['session'], selected['ref'], selected['state'])).fetchone()
        if row is None or row['delivered'] != 0 or row['holder_pid'] != selected['holder_pid'] \
                or row['holder_start'] != selected['holder_start'] or row['ref'] in states:
            return 0
        if remote._holder_is_me(row['holder_pid']) or remote._holder_alive(row['holder_pid'], row['holder_start']):
            return 0
        latest = _expired_observation(conn, row, str(source), now, remote)
        if latest is None:
            return 0
        profile, notice_id = remote._stale_notice_id(row, {})
        changed = conn.execute(
            "UPDATE notices SET delivered=?,holder_pid=NULL,holder_start=NULL,legacy_seen=1,settled='expired', "
            'notice_key=IFNULL(notice_key,?) WHERE session=? AND ref=? AND state=? AND delivered=0 '
            'AND holder_pid IS ? AND holder_start IS ?',
            (remote.SETTLED, notice_id, row['session'], row['ref'], row['state'], row['holder_pid'], row['holder_start']))
        if changed.rowcount != 1:
            return 0
        remote._swept_row(conn, (str(source), profile, row['session'], notice_id), 'expired')
        conn.commit()
        committed = True
    except (sqlite3.Error, OSError, TypeError, ValueError):
        remote.logger.debug('remote kanban absent-claim expiry unavailable', exc_info=True)
        return 0
    finally:
        if not committed and conn.in_transaction:
            conn.rollback()
    remote._log_dropped({'id': row['ref'].rsplit('/', 1)[-1]}, row['ref'], row['state'], row['session'], latest, now)
    return 1
