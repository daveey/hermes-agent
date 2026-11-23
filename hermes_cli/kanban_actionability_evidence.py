"""Fault-contained, read-only reuse of lifecycle evidence; never a policy writer. [daveey]"""
from __future__ import annotations

import json
import sqlite3

EVIDENCE_ERRORS = (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, OverflowError)


class Rows:
    """Give legacy policy readers named rows without mutating a caller's connection factory."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        cursor = self.conn.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, params)


def effective(reader, tid):
    """Return (pending stop, execution restriction, unknown). Arrival order is LOCAL evidence."""
    from hermes_cli import kanban_spend_decision as decision
    conn = Rows(reader.conn)
    try:
        stop = decision.pending_episode(conn, tid)
        restricted = decision.execution_restricted(conn, tid)
        if stop:
            stop = {**stop, 'data': json.loads(stop['payload'] or '{}')}
            if not isinstance(stop['data'], dict):
                return None, False, True
            if restricted:
                from hermes_cli.kanban_refusal_receipt import restricted as safety
                stop['data']['cause'] = 'safety_refusal' if safety(conn, tid) else 'scope_stopped'
        return stop, restricted, False
    except EVIDENCE_ERRORS:
        return None, False, True


def fields(conn, task, *, replica=False):
    """Optional per-card carriage. No whole-report scan, refresh or admission side effect."""
    from hermes_cli.kanban_actionability_sources import describe
    try:
        row = describe(conn, task.id)
    except EVIDENCE_ERRORS:
        from hermes_cli.kanban_actionability import project
        row = project({'task': {'id': task.id, 'status': task.status, 'assignee': task.assignee},
                       'unknown': True}, now=0)
        row['violations'] = ['evidence_unavailable']
        row.update(action='repair_lifecycle_contract', instruction='Recover authoritative task evidence')
    if replica and row['action'] != 'no_open_action':
        row['evidence_state'] = 'unknown'
    return {'actionability': row}


def render(row):
    """Bounded prompt/status line, not a generated executable original-scope instruction."""
    return (f"Actionability: {row['action']}; owner={row['owner']}; "
            f"{row['instruction']}; deadline={row['escalation']['at']} "
            f"(null=unrecorded); evidence={row['evidence_state']}; "
            f"violations={','.join(row['violations']) or 'none observed'}")[:1800]


def context(lines, conn, task):
    lines.append(render(fields(conn, task)['actionability']))
