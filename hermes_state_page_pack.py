"""Pack a bounded ranked page into one SQLite/Python row handoff. [daveey]

sqlite3 releases the GIL while stepping AND converting columns. A wide 200-row
page can therefore convoy thousands of times under unrelated serve CPU work.
This is only a transport for an already bounded page in the caller's snapshot;
it neither caches values nor changes filtering, ranking or authority.
"""
import json


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def read_page(conn, sql, params):
    """Return dict rows with SQLite types, order and binary64 values preserved."""
    return _read(conn, sql, params, None)


def read_chunks(conn, sql, params):
    """Hydrate an unbounded pin set in 64-row transport cells, without truncation.

    The original query still executes once; unlike LIMIT/OFFSET batching this
    does not repeat its sort or change snapshots between chunks.
    """
    return _read(conn, sql, params, 64)


def _read(conn, sql, params, chunk):
    # Prepare only: LIMIT 0 never steps the projection or its correlated probes.
    names = [column[0] for column in conn.execute(f'SELECT * FROM ({sql}) LIMIT 0', params).description]
    cells = []
    for name in names:
        column = _quoted(name)
        # JSON numbers round doubles to 15 digits. Transport REAL as SQLite's
        # round-trip decimal text and BLOB as hex, distinguished by storage type.
        cells.append(f"json_array(typeof({column}), CASE typeof({column}) "
                     f"WHEN 'real' THEN printf('%!.26g', {column}) "
                     f"WHEN 'blob' THEN hex({column}) ELSE {column} END)")
    prefix = f"WITH _packed_rows AS MATERIALIZED ({sql}) "
    source, suffix = '_packed_rows', ''
    if chunk:
        # Number the materialized, already ordered rows before grouping; never
        # serialize the entire pin history into one JSON value.
        prefix += (", _numbered AS MATERIALIZED (SELECT *, "
                   "row_number() OVER () - 1 AS _pack_ordinal FROM _packed_rows) ")
        source = '_numbered'
        suffix = f' GROUP BY _pack_ordinal / {chunk} ORDER BY MIN(_pack_ordinal)'
    cursor = conn.execute(prefix + f"SELECT json_group_array(json_array({','.join(cells)})) "
                          f"FROM {source}" + suffix, params)
    packed_rows = cursor.fetchall() if chunk else [cursor.fetchone()]
    return [dict(zip(names, (_value(kind, value) for kind, value in row)))
            for packed in packed_rows for row in json.loads(packed[0])]


def _value(kind, value):
    if kind == 'real':
        return float(value)
    if kind == 'blob':
        return bytes.fromhex(value)
    return value
