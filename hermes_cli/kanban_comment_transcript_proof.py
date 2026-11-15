"""Read-only corroboration of legacy comment references, never acknowledgement. [daveey]

Old ``commented`` events carry only the writer's wire-local ``comment_id`` and
no portable ``comment_uid``. When the receiver's row was stamped seconds
before the event, the one-second legacy fallback finds nothing and the unread
guard refuses with "data unavailable" forever. The writer's own retained
session transcript can bridge that gap: its ``kanban_comment`` call holds the
exact body and its paired successful result holds the source integer.

Every step is fail-closed. Coverage is deliberately limited to direct
``kanban_comment`` calls whose result is a top-level JSON object. Wrapped
``tool_call`` batches, ``cross_task`` comment actions, shell calls and redacted
bodies are not interpreted (no recursive search of arbitrary JSON); a direct
receipt of the same source integer under any other tool name refuses.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing

from hermes_constants import get_hermes_home
from hermes_cli.sqlite_safe_read import connect_tracked

MAX_MESSAGES = 10000


def messages(session):
    """Missing, pruned or unavailable source evidence leaves the guard closed."""
    path = get_hermes_home() / 'state.db'
    try:
        with closing(connect_tracked(path.as_uri() + '?mode=ro', tracking_path=path, uri=True, timeout=0.1)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT role,tool_call_id,tool_calls,content,timestamp FROM messages '
                'WHERE session_id=? ORDER BY timestamp LIMIT ?', (session, MAX_MESSAGES + 1),
            ).fetchall()
        return rows if len(rows) <= MAX_MESSAGES else []
    except (sqlite3.Error, OSError, ValueError):
        return []


class _Unprovable(Exception):
    """Retained identity cannot be established; the event stays unresolved."""


def _declarations(transcript):
    """Every assistant call declaration by ID, across all messages and tools.

    Uniqueness is established *before* any task/body filtering, so a conflicting
    declaration for another task or tool cannot be filtered away and leave the
    remaining one to borrow the single reply. Unparseable declarations make
    uniqueness unknowable.
    """
    found = {}
    for message in transcript:
        if message['role'] != 'assistant' or not message['tool_calls']:
            continue
        calls = json.loads(message['tool_calls'])
        if not isinstance(calls, list) or not all(isinstance(c, dict) for c in calls):
            raise _Unprovable
        for call in calls:
            found.setdefault(call.get('id'), []).append((message, call))
    return found


def _is_receipt(reply, task_id, source_id):
    """A successful top-level comment result for this task and source integer.

    Tool output is often plain text, so a non-JSON reply is simply not a receipt.
    """
    try:
        result = json.loads(reply['content'] or '')
    except (ValueError, TypeError):
        return False
    return (isinstance(result, dict) and result.get('ok') is True and result.get('task_id') == task_id
            and type(result.get('comment_id')) is int and result['comment_id'] == source_id)


def resolve(rows, event, payload, transcript, *, board=None):
    """Bridge source integer to exact body, never to a receiver integer.

    Exactly one successful receipt for (task, source integer) may exist in the
    retained transcript: a second one is conflicting source identity even when
    its body is absent from the receiver, so it refuses before any row lookup.
    Its call ID must be declared exactly once and replied to exactly once.
    Integer timestamps are floors of the retained call/result interval; no
    lateness window is added. Duplicate receiver bodies are refused. ``board``
    is the checking board's slug; a call naming any other board never proves.
    """
    source_id = payload.get('comment_id')
    author = payload.get('author')
    if type(source_id) is not int or not isinstance(author, str) or not author.strip():
        return []
    try:
        declared = _declarations(transcript)
        replies = [r for r in transcript if r['role'] == 'tool']
        receipts = [r for r in replies if _is_receipt(r, event['task_id'], source_id)]
        if len(receipts) != 1:
            return []
        reply = receipts[0]
        call_id = reply['tool_call_id']
        if (not isinstance(call_id, str) or not call_id
                or sum(1 for r in replies if r['tool_call_id'] == call_id) != 1
                or len(declared.get(call_id, [])) != 1):
            return []
        message, call = declared[call_id][0]
        function = call.get('function')
        if not isinstance(function, dict) or function.get('name') != 'kanban_comment':
            return []
        args = function.get('arguments')
        args = json.loads(args) if isinstance(args, str) else args
        if (not isinstance(args, dict) or args.get('task_id') != event['task_id']
                or not isinstance(args.get('body'), str)
                or args.get('board') not in (None, '') and args['board'] != board):
            return []
        start, end = math.floor(message['timestamp']), math.floor(reply['timestamp'])
        if not start <= event['created_at'] <= end:
            return []
        candidates = [r for r in rows if r['body'] == args['body'].strip()
                      and r['author'] == author.strip()
                      and start <= r['created_at'] <= event['created_at']]
    except (_Unprovable, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return []
    return candidates if len(candidates) == 1 else []
