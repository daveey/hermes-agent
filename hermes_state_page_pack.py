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
    cells = [_cell(_quoted(name)) for name in names]
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


def read_rows(conn, sql, params, width):
    """``conn.execute(sql, params).fetchall()`` as tuples in ONE statement and one handed-out row. [daveey]

    For a reader whose ``width`` result columns are known: no prepare-only probe for their names, the columns are
    renamed by position (a wrong ``width`` fails at prepare). Same transport as ``read_page``: SQLite types and row
    order are preserved; REAL travels as ``printf('%!.26g')`` text, whose round trip is exact only as far as the
    SQLite build's printf is (this runtime's is; Apple's system SQLite 3.51.0 was 1 ULP off on some doubles, and
    -0.0 arrives as 0.0). ``test_kanban_external_read_rows`` guards this interpreter's SQLite."""
    cols = [f"_pack_c{i}" for i in range(width)]
    packed = conn.execute(
        f"WITH _packed_rows({','.join(cols)}) AS MATERIALIZED ({sql}) "
        f"SELECT json_group_array(json_array({','.join(_cell(c) for c in cols)})) FROM _packed_rows",
        params).fetchone()
    return [tuple(_value(kind, value) for kind, value in row) for row in json.loads(packed[0])]


def _cell(column):
    # JSON numbers round doubles to 15 digits. Transport REAL as SQLite's
    # round-trip decimal text and BLOB as hex, distinguished by storage type.
    return (f"json_array(typeof({column}), CASE typeof({column}) "
            f"WHEN 'real' THEN printf('%!.26g', {column}) "
            f"WHEN 'blob' THEN hex({column}) ELSE {column} END)")


def _value(kind, value):
    if kind == 'real':
        return float(value)
    if kind == 'blob':
        return bytes.fromhex(value)
    return value
