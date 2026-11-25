"""Semantic state.db input of the annotation COUNTS part. [daveey]

Only session existence and canonical continuation edges can change the aliases
selected by the origin projector. Transcript content, token counts and turn
leases are not inputs. Use the canonical SQL rather than duplicating its branch,
delegate/reset, source, ended-state, message recency and started-at ordering.
An unreadable/missing/unknown schema raises: the holder must read afresh.
"""
from __future__ import annotations

from pathlib import Path


def key(path: Path) -> tuple:
    from hermes_cli.kanban_session_activity import cached_ro
    from hermes_state_compression import _CHAIN_CAP, _CHAIN_STEP_SQL

    with cached_ro(path, busy_budget=0) as conn:
        conn.execute('BEGIN')
        try:
            # Compile every canonical dependency even with no compressed rows;
            # an incomplete schema must not certify a reusable empty graph.
            conn.execute(_CHAIN_STEP_SQL, (None,)).fetchone()
            sessions = tuple(conn.execute('SELECT id, end_reason FROM sessions ORDER BY id'))
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
