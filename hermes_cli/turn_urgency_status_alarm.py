"""Detect silent turn-observer outages on the gateway's normal maintenance tick. [daveey]

Only the default primary board files host incidents. The observer writes local
status; this consumer owns Kanban writes, independently of admission scoring.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from hermes_cli.kanban_turn_urgency import _finite_clock

_log = logging.getLogger(__name__)
DETECTOR = 'turn-urgency-detector'
KEY_PREFIX = 'turn-urgency-unknown:'
GRACE_SECONDS = 600
REPEAT_SECONDS = 3600


def _home():
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _enabled():
    if sys.platform != 'linux':
        return False
    # The installed account service does not publish into named profile homes.
    if _home().resolve() != (Path.home() / '.hermes').resolve():  # health: allow HX001 -- account systemd service target
        return False
    # Read systemd's actual enablement link; an installed unit alone is not enabled.
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return (config / 'systemd/user/timers.target.wants/hermes-turn-urgency.timer').is_file()


def _default_board(conn):
    from hermes_cli.kanban_db_sync import board_slug_for_path
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return bool(path) and board_slug_for_path(path) == 'default'


def _read(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(128 * 1024 + 1)
        value = json.loads(raw) if len(raw) <= 128 * 1024 else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix='.turn-alarm-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _clock(value, now):
    return value if _finite_clock(value) and 0 <= value <= now else None


def _health(home, now):
    """Healthy empty owners differ from missing, malformed or expired cache."""
    path = home / 'cache/kanban-turn-urgency.json'
    cache = _read(path)
    status = _read(path.with_suffix('.status.json'))
    if status.get('version') != 1 or status.get('board') != 'default':
        status = {}
    observed = _clock(cache.get('observed_at'), now)
    until = cache.get('valid_until')
    valid = (type(cache.get('version')) is int and cache['version'] == 1
             and cache.get('board') == 'default'
             and isinstance(cache.get('source_revision'), str) and bool(cache['source_revision'].strip())
             and observed is not None and _finite_clock(until)
             and observed < until <= observed + 120
             and isinstance(cache.get('owners'), list) and len(cache['owners']) <= 4096)
    if valid:
        for owner in cache['owners']:
            if (not isinstance(owner, dict)
                    or not all(isinstance(owner.get(k), str) and owner[k].strip()
                               for k in ('task_id', 'source', 'ref', 'head'))
                    or owner['source'] not in ('core-lane', 'state-turn')
                    or not _finite_clock(owner.get('expires_at')) or owner['expires_at'] < until):
                valid = False
                break
    success = _clock(status.get('last_success_at'), now)
    first = _clock(status.get('first_failure_at'), now)
    # A started/in-flight tick does not invalidate a previous fresh observation.
    if valid and now < until:
        return True, observed, None
    # Only structurally valid publications prove success, even after expiry.
    successes = [value for value in (success, observed if valid else None) if value is not None]
    since = max(successes) if successes else first
    error = status.get('last_error')
    if not isinstance(error, str) or not error:
        error = 'missing cache' if not path.exists() else 'expired cache' if observed is not None else 'invalid cache'
    return False, since, error.splitlines()[-1][:512]


def _stamp(now):
    return datetime.fromtimestamp(now, UTC).isoformat()


def _last_event(conn, card):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'turn_urgency_alarm' "
                       "ORDER BY id DESC LIMIT 1", (card,)).fetchone()
    return json.loads(row[0]) if row else {}


def _comment(conn, card, now, recovered, body):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_comment_ids import add_comment
    with kb.write_txn(conn):
        add_comment(conn, card, DETECTOR, body, system=True)
        kb._append_event(conn, card, 'turn_urgency_alarm', {'at': now, 'recovered': recovered})


def _incident(conn, host, now, since, error, healthy):
    from hermes_cli import kanban_db as kb
    prefix = f'{KEY_PREFIX}{host}:'
    rows = conn.execute('SELECT id, status FROM tasks WHERE idempotency_key >= ? AND idempotency_key < ? '
                        'ORDER BY created_at DESC, id DESC', (prefix, prefix + '\U0010ffff')).fetchall()
    opened = next((row for row in rows if row['status'] not in ('done', 'archived')), None)
    row = opened or (rows[0] if rows else None)
    if healthy:
        if row and not _last_event(conn, row['id']).get('recovered'):
            _comment(conn, row['id'], now, True, f'Turn-urgency observer recovered at {_stamp(now)} on {host}.')
        return None
    if now - since < GRACE_SECONDS:
        return None
    detail = (f'No successful turn-urgency observation on {host} since {_stamp(since)}.\n'
              f'Last error: {error}\n\n'
              'Read: `journalctl --user -u hermes-turn-urgency.service --since "30 minutes ago"`.\n'
              'Inspect ~/.hermes/cache/kanban-turn-urgency.status.json and the owner cache. '
              'Repair the observer or its source access; do not change admission scoring. '
              'This is agent-fixable work, not a human decision. Preserve any deliberate quota containment; '
              'coordinate with its repair owner before restarting timers.')
    if opened:
        card = opened['id']
        last = _last_event(conn, card)
        if not last or last.get('recovered'):
            # Heal a create/event crash and re-arm recovery without adding an
            # extra failure comment inside the hourly repeat window.
            last = {'at': last.get('at', now), 'recovered': False}
            with kb.write_txn(conn):
                kb._append_event(conn, card, 'turn_urgency_alarm', last)
        if now - last['at'] >= REPEAT_SECONDS:
            _comment(conn, card, now, False, f'Turn-urgency failure still present at {_stamp(now)}.\n{detail}')
        return card
    from hermes_cli.kanban_triage_sweep import _supervisor_profile
    card = kb.create_task(conn, title=f'Turn-urgency observer has no successful observation on {host} for 10 min',
                          body=detail, assignee=_supervisor_profile(), created_by=DETECTOR,
                          workspace_kind='scratch', requires=['terminal', 'file', 'git', 'hub:write'],
                          runs_on=['pool:worker', 'capability:hub-write'],
                          idempotency_key=f'{prefix}{now.hex()}', priority=-1)
    with kb.write_txn(conn):
        kb._append_event(conn, card, 'turn_urgency_alarm', {'at': now, 'recovered': False})
    return card


def sweep(conn, *, now=None):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    if not _default_board(conn) or sync.board_mode(conn) != 'primary' or not _enabled():
        return None
    import fcntl
    now = float(time.time() if now is None else now)
    if not _finite_clock(now):
        return None
    home = _home()
    directory = home / 'cache'
    directory.mkdir(parents=True, exist_ok=True)
    # Multiple gateway/profile maintenance processes share a host-level receipt.
    with (directory / '.turn-urgency-alarm.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        path = directory / 'turn-urgency-alarm.json'
        receipt = _read(path)
        healthy, since, error = _health(home, now)
        if since is None:
            since = _clock(receipt.get('since'), now)
        if since is None:
            since = now
        _write(path, {'since': now if healthy else since})
        return _incident(conn, wn._own(conn), now, since, error, healthy)


def safe_sweep(conn):
    try:
        return sweep(conn)
    except Exception:  # health: allow BLE001 -- alarm failure must not stop dispatch
        _log.warning('kanban dispatcher: turn-urgency alarm sweep failed', exc_info=True)
        return None
