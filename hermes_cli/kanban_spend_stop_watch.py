"""Model-free backstop for spend stops nobody reviewed or routed. [daveey]"""
from __future__ import annotations

import argparse
import sqlite3
import time
from typing import cast

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli.kanban_spend_caused_review import effective_cause
from hermes_cli.kanban_wake_check import foreign_ids

AUTHOR = "spend-stop-watch"
FILED = "spend_stop_watch_filed"


def stale_stops(conn: sqlite3.Connection, *, now: float, older_than: int = 1800) -> list[dict]:
    """Read home triage stops strictly older than the threshold, without a disposition."""
    copies = foreign_ids(conn)
    result = []
    for row in conn.execute("SELECT id,title,tenant,created_by FROM tasks WHERE status='triage'").fetchall():
        tid = row['id']
        if tid in copies or row['created_by'] in (AUTHOR, 'kanban-spend-budget'):
            continue
        stop = decision.pending_episode(conn, tid)
        if stop is None or stop['created_at'] >= now - older_than:
            continue
        episode = cast(str, reviewer.episode_of(stop))
        if reviewer.state(conn, tid, episode) is not None or _routed(conn, tid, episode):
            continue
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
                        "AND json_extract(payload,'$.episode')=?", (tid, FILED, episode)).fetchone():
            continue
        result.append({**dict(row), 'episode': episode, 'stop': stop})
    return result


def _routed(conn: sqlite3.Connection, tid: str, episode: str) -> bool:
    """A decision card, or the notice's live chat route. ``unavailable`` means nobody was told."""
    route = decision.episode_route(conn, tid, episode)
    if route is None:
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
                           "AND json_extract(payload,'$.episode')=? ORDER BY id DESC LIMIT 1",
                           (tid, decision.NOTICE, episode)).fetchone()
        route = kb._json_dict(row[0]).get('producer') if row else None
    return isinstance(route, dict) and route.get('kind') != 'unavailable' and decision._live_route(conn, route)


def _file(conn: sqlite3.Connection, card: dict, now: float) -> str:
    tid, episode, stop = card['id'], card['episode'], card['stop']
    minutes = int((now - stop['created_at']) // 60)
    cause = effective_cause(conn, tid, stop) or 'run budget'
    stopped = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(stop['created_at']))
    from hermes_cli.kanban_model_neutral import forwarded_text
    action = ("Safety refusal stays held: escalate to David for a decision; no automatic retry or release. "
              if cause == 'safety_refusal' else
              f"Decide {tid}: use `hermes kanban release {tid}` from an authorized owning chat, "
              "archive it if no longer wanted, or escalate to its owner if authority is missing. ")
    body = (f"Card {tid} stopped at {stopped}. Stop cause: {cause}.\n"
            "Source title (historical evidence):\n" + forwarded_text(card['title']) + "\n\n"
            "Neither the spend reviewer nor an owner route picked up this stop.\n" + action +
            "Worker shells cannot release it directly; do not rerun unchanged work or bypass a safety hold.")
    new_id = kb.create_task(conn, title=f"Spend stop on {tid} undecided for {minutes} min: no review, no route",
                            body=body, assignee='default', created_by=AUTHOR, workspace_kind='scratch',
                            tenant=card['tenant'], idempotency_key=f'{AUTHOR}:{tid}:{episode}')
    kb._append_event(conn, tid, FILED, {'episode': episode, 'task_id': new_id})
    return new_id


def watch(conn: sqlite3.Connection, *, now: float | None = None) -> list[str]:
    """File once per episode, atomically with a durable receipt; refusal remains retryable."""
    now = time.time() if now is None else now
    if not stale_stops(conn, now=now):
        return []
    filed = []
    # Re-read under the writer lock: two cron fires cannot both publish a card.
    with kb.write_txn(conn):
        for card in stale_stops(conn, now=now):
            try:
                # A refused create may already have inserted rows; roll back its savepoint.
                with kb.write_txn(conn, allow_nested=True):
                    new_id = _file(conn, card, now)
            except (ValueError, PermissionError):
                continue
            filed.append(new_id)
    return filed


def command(args: argparse.Namespace) -> int:
    """No-agent CLI: silent on no-op, one stable receipt line per filed card."""
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        for new_id in watch(conn):
            row = conn.execute("SELECT task_id,payload FROM task_events WHERE kind=? "
                               "AND json_extract(payload,'$.task_id')=?", (FILED, new_id)).fetchone()
            episode = kb._json_dict(row['payload'])['episode']
            print(f"filed {new_id} for {row['task_id']} stop {episode}")
    return 0
