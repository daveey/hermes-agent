"""Detached sidebar elapsed clocks; timestamps never imply execution authority. [daveey]"""
import json
import math
import sqlite3

from hermes_cli.kanban_open_asks import card_is_open_human_ask
from hermes_cli.kanban_table_columns import table_columns

CARDS_CAP = 6
PREFIX = 'timed:'


def _epoch(value, *, milliseconds=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = value / 1000 if milliseconds else value
    return value if math.isfinite(value) and value > 0 else None


def select(conn, alias='t'):
    """Tolerant scalar selections for both local boards and older replicas."""
    columns = table_columns(conn, 'tasks')
    runs = table_columns(conn, 'task_runs')
    events = table_columns(conn, 'task_events')
    field = lambda key: f'{alias}.{key}' if key in columns else 'NULL'
    start = ('(SELECT r.started_at FROM task_runs r WHERE r.id = '
             f'{alias}.current_run_id AND r.task_id = {alias}.id)'
             if 'current_run_id' in columns and {'id', 'task_id', 'started_at'} <= runs else 'NULL')
    # Unary + preserves values but disables SQLite's MAX timestamp-index walk:
    # prefer the selective (kind, task_id) lookup over unrelated newer history.
    blocked = (f"(SELECT MAX(+e.created_at) FROM task_events e WHERE e.task_id = {alias}.id AND e.kind = 'blocked')"
               if {'task_id', 'kind', 'created_at'} <= events else 'NULL')
    return ', '.join(f'{expression} AS {key}' for key, expression in (
        ('timer_run_start', start), ('timer_block_start', blocked),
        ('timer_worker_start', field('worker_started_at')), ('timer_updated', field('updated_at'))))


def card_timer(card):
    """Current run first; blocked event first, then Board ready_since (ms), then updated_at (s)."""
    status = card.get('status')
    if card.get('stale') or card.get('partial_source_unknown'):
        return None
    if status == 'running':
        state = 'running'
        since = (_epoch(card.get('timer_run_start')) or _epoch(card.get('started_at'))
                 or _epoch(card.get('timer_worker_start')))
    elif card_is_open_human_ask(status, card.get('block_kind'), card.get('human_request')):
        state = 'blocked'
        since = (_epoch(card.get('timer_block_start')) or _epoch(card.get('ready_since'), milliseconds=True)
                 or _epoch(card.get('timer_updated')))
    elif status == 'scheduled' and isinstance(card.get('timer_producer_wait'), dict):
        state = 'waiting'
        since = _epoch(card['timer_producer_wait'].get('since'))
    elif status == 'scheduled' and isinstance(card.get('timer_external_wait'), dict):
        state = 'waiting'
        since = _epoch(card['timer_external_wait'].get('waiting_since'))
    else:
        return None
    if since is None:
        return None
    result = {'id': str(card.get('key') or card['id']), 'title': str(card.get('title') or ''),
              'state': state, 'since': since}
    if state == 'waiting' and card.get('timer_external_wait'):
        result['wait_kind'] = card['timer_external_wait']['wait_kind']
    return result


def marker(card):
    value = card_timer(card)
    return {(value['id'], PREFIX + json.dumps(value, sort_keys=True), False)} if value else set()


def local(conn, pairs):
    """Capture once in the count transaction, no extra board opens. Failure omits clocks only."""
    try:
        ids = sorted({tid for _, tid in pairs})
        cols = table_columns(conn, 'tasks')
        fields = ', '.join(f't.{key}' if key in cols else f'NULL AS {key}' for key in (
            'id', 'title', 'status', 'started_at', 'block_kind', 'human_request', 'ready_since'))
        names = [part[0] for part in conn.execute(f'SELECT {fields}, {select(conn)} FROM tasks t LIMIT 0').description]
        found = {str(row[0]): dict(zip(names, row)) for row in conn.execute(
            f'SELECT {fields}, {select(conn)} FROM tasks t WHERE t.id IN (SELECT value FROM json_each(?))',
            (json.dumps(ids),))}
        from hermes_cli.kanban_producer_wait import stamp
        stamp(conn, list(found.values()))
        from hermes_cli.kanban_triage_attention import annotate
        annotate(conn, found.values())
        return found
    except (sqlite3.Error, TypeError, ValueError):
        return {}


def stamp(row, markers):
    """Deduplicate lineage/owner keys, blue first, oldest within a colour; clear empty fields."""
    cards = {}
    for _, bucket, _ in markers:
        if bucket.startswith(PREFIX):
            value = json.loads(bucket[len(PREFIX):])
            cards[value['id']] = value
    ordered = sorted(cards.values(), key=lambda c: (c['state'] != 'running', c['since'], c['id']))
    if ordered:
        row['kanban_timed_cards'] = ordered[:CARDS_CAP]
        row['kanban_timed_more'] = max(0, len(ordered) - CARDS_CAP)
    else:
        row.pop('kanban_timed_cards', None)
        row.pop('kanban_timed_more', None)
