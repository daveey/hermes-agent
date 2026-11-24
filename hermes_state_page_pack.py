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
    packed = conn.execute(f"WITH _packed_rows AS MATERIALIZED ({sql}) "
                          f"SELECT json_group_array(json_array({','.join(cells)})) "
                          "FROM _packed_rows", params).fetchone()[0]
    return [dict(zip(names, (_value(kind, value) for kind, value in row)))
            for row in json.loads(packed)]


def _value(kind, value):
    if kind == 'real':
        return float(value)
    if kind == 'blob':
        return bytes.fromhex(value)
    return value
