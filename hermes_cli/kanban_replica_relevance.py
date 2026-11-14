"""Which replica tasks cannot affect this gateway's emitted cards. [daveey]"""
from __future__ import annotations

import itertools
from hermes_cli.kanban_replica_transport import iter_page


def fetch_payloads(conn, identities, version_column: str, page_size: int = 100):
    """Yield raw seven-tuples for identities inside the caller's open read snapshot.

    Does not begin, commit or close the transaction. Each page is one SQLite result,
    avoiding per-task GIL handoffs. The only interpolated column is allowlisted.
    """
    if version_column not in ('version', 'NULL'):
        raise ValueError('invalid replica version column')
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError('replica refetch page size must be between 1 and 100')
    encoding = conn.execute('PRAGMA encoding').fetchone()[0]
    pending = iter(identities)
    while batch := tuple(itertools.islice(pending, page_size)):
        where = ' OR '.join('(gw = ? AND key = ?)' for _ in batch)
        args = tuple(value for pair in batch for value in pair)
        packed = conn.execute(
            'SELECT CAST(json_group_array(json_array(gw, key, data, updated_at, '
            f"{version_column}, seq, octet_length(data))) AS BLOB) FROM replica WHERE coll = 'tasks' AND ({where})",
            args,
        ).fetchone()[0]
        yield from iter_page(packed, encoding)
        del packed


def is_bystander(record: dict, key: str, me: str, legacy_keys) -> bool:
    """Conservative gateway-wide relevance; ``legacy_keys`` belongs to the row's gateway.

    A qualified twin of any legacy row remains relevant: alias folding may add local
    links without changing the qualified row's seq. No profile/rel filter is safe here.
    """
    parts = key.split('/')
    if len(parts) != 3 or not all(parts) or '/'.join(parts[1:]) in legacy_keys:
        return False
    if record.get('origin_bindings'):
        return False
    sessions = record.get('sessions')
    if not isinstance(sessions, list):
        return True
    return not any(isinstance(link, dict) and link.get('gw') == me for link in sessions)
