"""Semantic state.db input of the annotation COUNTS part. [daveey]

Only session existence and canonical continuation edges can change the aliases
selected by the origin projector. Transcript content, token counts and turn
leases are not inputs. Use the canonical SQL rather than duplicating its branch,
delegate/reset, source, ended-state, message recency and started-at ordering.
An unreadable/missing/unknown schema raises: the holder must read afresh.
"""
from __future__ import annotations

import json
from pathlib import Path


def _session_rows(conn) -> tuple:
    """Transport the ordered TEXT/NULL inventory without one GIL handoff per session.

    Nontext legacy cells retain the literal SQLite path in this same transaction;
    JSON must never reinterpret BLOBs, nonfinite REALs or signed zero as IDs.
    """
    count, packed = conn.execute(
        "SELECT COUNT(*), json_group_array(json_array(id, end_reason)) FILTER (WHERE "
        "typeof(id) IN ('text','null') AND typeof(end_reason) IN ('text','null')) "
        "FROM (SELECT id, end_reason FROM sessions ORDER BY id)").fetchone()
    rows = json.loads(packed)
    if len(rows) != count:
        return tuple(conn.execute('SELECT id, end_reason FROM sessions ORDER BY id'))
    return tuple(tuple(row) for row in rows)


def key(path: Path) -> tuple:
    from hermes_cli.kanban_session_activity import cached_ro
    from hermes_state_compression import _CHAIN_CAP, _CHAIN_STEP_SQL

    with cached_ro(path, busy_budget=0) as conn:
        conn.execute('BEGIN')
        try:
            # Compile every canonical dependency even with no compressed rows;
            # an incomplete schema must not certify a reusable empty graph.
            conn.execute(_CHAIN_STEP_SQL, (None,)).fetchone()
            sessions = _session_rows(conn)
            edges = {}
            for sid, reason in sessions:
                if reason == 'compression':
                    row = conn.execute(_CHAIN_STEP_SQL, (sid,)).fetchone()
                    if row is not None:
                        edges[sid] = row[0]
            # Unknown lineage is not an unchanged successful projection. Match
            # the resolver's cycle/cap refusal, including a tail at the cap.
            for root in edges:
                sid, seen = root, {root}
                for _ in range(_CHAIN_CAP):
                    if sid not in edges:
                        break
                    sid = edges[sid]
                    if sid in seen:
                        raise ValueError('origin_lineage_invalid')
                    seen.add(sid)
                else:
                    raise ValueError('origin_lineage_invalid')
            return tuple(sid for sid, _ in sessions), tuple(sorted(edges.items()))
        finally:
            conn.rollback()
