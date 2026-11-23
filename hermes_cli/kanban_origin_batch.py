"""Invocation-local exact owner task reads for origin-ledger projection.

No cross-call memo and no write-stamp lifetime: each phase reads its own current
snapshot, including a fresh read after the ledger write lock is acquired.
"""
from __future__ import annotations

import json
import os


def task_rows(source, bindings):
    """None selects the legacy keyed-query path; errors never become absence."""
    enabled = os.environ.get('HERMES_ORIGIN_BATCH_READS', '1').lower() not in ('0', 'false', 'off', 'no')  # health: allow HX002 -- reversible emergency read batching
    if not enabled:
        return None
    wanted = sorted({(b['owner']['gateway'], b['task_record_key']) for b in bindings})
    if not wanted or source is None:
        return {}
    return {(gw, key): (version, data) for gw, key, version, data in source.execute(
        "SELECT r.gw,r.key,r.version,r.data FROM json_each(?) AS wanted JOIN replica AS r "
        "ON r.gw=json_extract(wanted.value,'$[0]') AND r.coll='tasks' "
        "AND r.key=json_extract(wanted.value,'$[1]')", (json.dumps(wanted),)).fetchall()}


def lookup(source, binding, rows):
    key = binding['owner']['gateway'], binding['task_record_key']
    if rows is not None:
        return rows.get(key)
    return source.execute("SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?", key).fetchone()
