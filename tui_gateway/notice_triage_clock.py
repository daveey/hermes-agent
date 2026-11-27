"""Detached triage clocks for canonical outcome fanout; no lifecycle writes."""
from pathlib import Path

from hermes_cli import kanban_triage_attention as attention

_cache = {}
_remote_due = None


def reset():
    global _remote_due
    _cache.clear()
    _remote_due = None


def local(conn, ident, top, *, now):
    stat = Path(ident).stat()
    identity = (stat.st_dev, stat.st_ino)
    hit = _cache.get(ident)
    changed = hit is None or hit[0] != identity or top < hit[1]
    if not changed and top != hit[1]:
        changed = conn.execute(
            'SELECT 1 FROM task_events WHERE id > ? AND kind IN (SELECT value FROM json_each(?)) LIMIT 1',
            (hit[1], __import__('json').dumps(attention.LIFECYCLE_KINDS))).fetchone() is not None
    if changed:
        cards = [dict(id=tid, status='triage') for tid, in conn.execute("SELECT id FROM tasks WHERE status='triage'")]
        attention.annotate(conn, cards)
        acked = set() if hit is None or hit[0] != identity else hit[3]
    else:
        cards, acked = hit[2], hit[3]
    episodes = {(c['id'], c.get('triage_entered_at')) for c in cards}
    acked &= episodes
    _cache[ident] = (identity, top, cards, acked)
    ready = {c['id'] for c in cards if attention.overdue(c, now=now) and (c['id'], c.get('triage_entered_at')) not in acked}
    due = min((at for c in cards if (at := attention.next_transition(c, now=now)) is not None), default=None)
    return ready, due


def ack(ident, tids):
    hit = _cache.get(ident)
    if hit:
        hit[3].update((c['id'], c.get('triage_entered_at')) for c in hit[2] if c['id'] in tids)


def remote_cards(cards, *, now):
    global _remote_due
    cards = list(cards)
    _remote_due = min((at for c in cards if (at := attention.next_transition(c, now=now)) is not None), default=None)
    return cards


def remote_due():
    return _remote_due
