"""Opt-in, bounded policy representation conversion, never startup maintenance.

Call on the existing SessionDB transaction owner. This is not reader discovery:
readers_compatible acknowledges independently established rollout custody. A batch
cursor is a scan position, NOT a downgrade-readiness receipt. Busy/oversize owners
must be revisited; disable new interned writes before a rollback scan.
"""
from typing import Any

from hermes_state_local import POLICY_PREFIX
from hermes_state_local_lineage import validate_local_lineage
from hermes_state_mutation_guards import MUTATION_GUARD_REFUSALS, require_idle
from hermes_state_policy_config import BLOB_PREFIX, _stored_reference, decode_receipt, store_receipt
from hermes_state_runtime import RuntimeStoreError


def convert_policy_config_batch(db, *, intern, apply=False, readers_compatible=False,
                                after='', limit=8, max_bytes=1024 * 1024):
    """Inspect at most limit keys and rewrite at most max_bytes of stored receipts.

    Dry-run uses the read path and performs no DML. Apply uses one existing owner
    transaction; an unreadable receipt rolls back the ENTIRE batch. No additional
    transaction control, checkpoints, VACUUM, config writes, runtime-epoch changes
    or background loop; ordinary owner commit/maintenance behavior is unchanged.
    Neither `more=False` nor `converted=0` proves all refs are gone: skipped rows
    and concurrent creates require a fresh pass after producer disable/quiescence.
    """
    if (type(intern) is not bool or type(apply) is not bool or
            type(limit) is not int or not 1 <= limit <= 32 or
            type(max_bytes) is not int or not 1 <= max_bytes <= 4 * 1024 * 1024 or
            not isinstance(after, str)):
        raise RuntimeStoreError('invalid_params')
    if apply and intern and readers_compatible is not True:
        raise RuntimeStoreError('runtime_coordination_required')

    def batch(conn):
        rows = conn.execute('SELECT key,length(CAST(value AS BLOB)) FROM state_meta '
                            'WHERE key GLOB ? AND key>? ORDER BY key LIMIT ?',
                            (POLICY_PREFIX + '*', POLICY_PREFIX + after, limit + 1)).fetchall()
        result: dict[str, Any] = dict(scanned=0, converted=0, bytes=0, skipped={}, after=after,
                                      more=len(rows) > limit, applied=apply)
        for key, size in rows[:limit]:
            sid = key[len(POLICY_PREFIX):]
            digest = None
            if size is None or size > max_bytes:
                result['scanned'] += 1
                result['after'] = sid
                result['skipped'][sid] = 'oversize'
                continue
            if result['bytes'] + size > max_bytes:
                result['more'] = True
                break
            raw_size = size
            if apply:
                digest = _stored_reference(conn, sid)
                if digest is not None:
                    blob = conn.execute('SELECT length(CAST(value AS BLOB)) FROM state_meta WHERE key=?',
                                        (BLOB_PREFIX + digest,)).fetchone()
                    if blob is None or not isinstance(blob[0], int):
                        raise RuntimeStoreError('storage_unavailable')
                    # Worst-case JSON escaping is six bytes per payload byte. Bound
                    # hydrated output BEFORE loading a potentially huge shared blob.
                    size += 6 * blob[0]
            if size > max_bytes:
                result['scanned'] += 1
                result['after'] = sid
                result['bytes'] += raw_size  # inspected raw bytes still spend the batch budget
                result['skipped'][sid] = 'oversize'
                continue
            if result['bytes'] + size > max_bytes:
                result['bytes'] += raw_size
                result['more'] = True
                break
            result['scanned'] += 1
            result['after'] = sid
            result['bytes'] += size
            if not apply:
                continue
            raw = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()[0]
            saved = decode_receipt(conn, raw)
            target = validate_local_lineage(conn, saved)
            try:
                require_idle(db, conn, list({sid, target}))
            except RuntimeStoreError as exc:
                if exc.reason not in ('session_busy', 'unknown_execution'):
                    raise
                result['skipped'][sid] = exc.reason
                continue
            except MUTATION_GUARD_REFUSALS:
                result['skipped'][sid] = 'session_busy'
                continue
            # Already in the requested representation: do not rewrite unchanged pages.
            if (digest is not None) == intern:
                continue
            store_receipt(conn, sid, saved, intern=intern)
            result['converted'] += 1
        return result

    if apply:
        return db._execute_write(batch)
    with db._read_ctx() as conn:
        return batch(conn)


def main(argv=None):
    """One finite maintenance batch; dry-run by default. No implicit activation."""
    import argparse
    from contextlib import closing
    import json
    from pathlib import Path
    import sqlite3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--direction', required=True, choices=('intern', 'inline'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--readers-compatible', action='store_true')
    parser.add_argument('--after', default='')
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--max-bytes', type=int, default=1024 * 1024)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error('--db must name an existing state database')
    if args.apply and args.direction == 'intern' and not args.readers_compatible:
        print(json.dumps({'error': 'runtime_coordination_required'}))
        return 2
    from hermes_state import SessionDB
    try:
        with closing(SessionDB(args.db, read_only=not args.apply)) as db:
            result = convert_policy_config_batch(db, intern=args.direction == 'intern', apply=args.apply,
                readers_compatible=args.readers_compatible, after=args.after,
                limit=args.limit, max_bytes=args.max_bytes)
    except (RuntimeStoreError, sqlite3.Error):
        # Never echo frozen config, request data or SQLite exception payloads.
        print(json.dumps({'error': 'storage_unavailable'}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
