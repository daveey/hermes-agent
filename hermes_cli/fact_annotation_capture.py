"""Detached operational annotation inputs; called only by the feed owner. [daveey]

Paths and profile identity come from authorized startup. No discovery, source
publication or legacy annotation admission occurs in these capture functions.
"""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import stat
import sqlite3
import time

from hermes_cli.fact_capture import CaptureBudgetExceeded


@contextmanager
def read_state(path, budget):
    from hermes_state import _connect_tracked_db
    from hermes_state_holders import read_only_db_uri
    deadline = time.monotonic() + budget.max_seconds
    with closing(_connect_tracked_db(read_only_db_uri(path), tracking_path=path,
                                     uri=True, isolation_level=None, timeout=0)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, budget.max_bytes)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute('BEGIN')
        yield conn
        if time.monotonic() >= deadline:
            raise CaptureBudgetExceeded('annotation state capture deadline exceeded')


def limited(cursor, budget):
    rows = []
    size = 0
    for row in cursor:
        size += len(repr(row).encode())
        if len(rows) >= budget.max_keys or size > budget.max_bytes:
            raise CaptureBudgetExceeded('annotation state capture exceeds inventory budget')
        rows.append(tuple(row))
    return rows


def read_json(path, budget, *, absent):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return absent
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('annotation input must be a regular file')
        raw = stream.read(budget.max_bytes + 1)
    if len(raw) > budget.max_bytes:
        raise CaptureBudgetExceeded('annotation file capture exceeds byte budget')
    return json.loads(raw.decode('utf-8-sig'))


def turns(path, budget, *, profile, gateway, now):
    from hermes_cli import turn_session_activity as turns
    from hermes_cli.turn_process_execution_observations import holder_digest
    from tui_gateway.turn_marker import auto_continue_bounds
    from hermes_cli.config_effective import load_profile_config_effective
    due = [now + 30]
    with read_state(path, budget) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                  " AND name IN ('session_turn_leases','state_meta','session_admissions','messages')")}
        leases = limited(conn.execute(
            'SELECT conversation_id,holder,acquired_at,expires_at FROM session_turn_leases'
            ' WHERE expires_at > ? LIMIT ?', (now, budget.max_keys + 1)), budget) if 'session_turn_leases' in tables else []
        live = set()
        if {'state_meta', 'session_admissions', 'messages'} <= tables:
            pauses = limited(conn.execute(
                'SELECT a.target_session_id,m.value FROM state_meta m CROSS JOIN session_admissions a'
                " ON a.admission_id=substr(m.key,?) WHERE m.key>=? AND m.key<? AND a.status='started' LIMIT ?",
                (len(turns._GATEWAY_CHECKPOINT_PREFIX) + 1, turns._GATEWAY_CHECKPOINT_PREFIX,
                 turns._GATEWAY_CHECKPOINT_END, budget.max_keys + 1)), budget)
            for sid, raw in pauses:
                try:
                    receipt = json.loads(raw)
                    message_id = int(receipt['max_message_id'])
                except (ValueError, TypeError, KeyError):
                    continue
                stamp = conn.execute('SELECT timestamp FROM messages WHERE id=?', (message_id,)).fetchone()
                if sid and stamp and stamp[0] is not None:
                    expires = float(stamp[0]) + turns._GATEWAY_PAUSE_FRESH_S
                    if now <= expires:
                        live.add(str(sid))
                        due.append(expires)
    data = read_json(Path(path).parent / 'desktop/interrupted_turns.json', budget, absent={})
    def bounds():
        return auto_continue_bounds(load_profile_config_effective(Path(path).parent / 'config.yaml'))
    live.update(turns.project_paused_tui_turns(data, bounds, now))
    by_session = {}
    for cid, holder, acquired, expires in leases:
        if cid and not turns._holder_dead(str(holder or '')):
            live.add(cid)
            by_session.setdefault(cid, []).append({'conversation_id': cid, 'holder_digest': holder_digest(holder),
                'acquired_at': acquired, 'expires_at': expires, 'profile': profile})
            due.append(float(expires))
    # Paused-turn eligibility is bounded by its producer policy. Reconcile its
    # wall boundary as well as leases, without a request-time config read.
    if isinstance(data, dict) and data:
        _, fresh, _ = bounds()
        for entry in data.values():
            if isinstance(entry, dict):
                try:
                    due.append(max(float(entry.get(k) or 0) for k in ('started_at', 'parked_at', 'paused_at')) + fresh)
                except (ValueError, TypeError):
                    continue
    return {'live': live, 'leases': by_session, 'gateway': gateway}, min((d for d in due if d > now), default=now + 30)


def children(path, budget, *, profile, gateway, now):
    from hermes_cli.subagent_session_activity import _WINDOW
    with read_state(path, budget) as conn:
        rows = limited(conn.execute(
            "SELECT id,parent_session_id,last_activity_at,started_at FROM sessions"
            " WHERE source='subagent' AND parent_session_id IS NOT NULL AND ended_at IS NULL"
            ' AND started_at >= ? LIMIT ?', (now - _WINDOW, budget.max_keys + 1)), budget)
    due = min((row[3] + _WINDOW for row in rows if row[3] + _WINDOW > now), default=now + 30)
    return {'by_profile': {profile: (rows, None)}, 'gateway': gateway}, min(due, now + 30)


def processes(path, budget, *, profile, gateway, now):
    from hermes_cli import process_session_activity as activity
    from tools.process_registry import process_registry
    entries = read_json(path, budget, absent=[])
    if not isinstance(entries, list):
        raise ValueError('invalid process checkpoint')
    if len(entries) > budget.max_keys:
        raise CaptureBudgetExceeded('process checkpoint exceeds key budget')
    owned, details, evidence = {}, {}, {}
    if not process_registry._lock.acquire(timeout=budget.max_seconds):
        raise CaptureBudgetExceeded('process registry lock capture deadline exceeded')
    try:
        if len(process_registry._running) > budget.max_keys:
            raise CaptureBudgetExceeded('process registry exceeds key budget')
        fields = ('session_key', 'pid_scope', 'pid', 'host_start_time', 'parent_session_id',
                  'owner_task_id', 'task_id', 'id', 'command', 'started_at')
        running = [{field: getattr(s, field) for field in fields}
                   for s in process_registry._running.values() if not s.exited]
    finally:
        process_registry._lock.release()
    for item in running:
        recorded = activity._registry_profile(item['session_key'])
        if recorded and recorded != profile:
            continue
        if item['pid_scope'] == 'host' and not activity._live(item['pid'], item['host_start_time']):
            continue
        owners = frozenset(activity._owner_ids(item))
        proc_id = item['id']
        evidence[proc_id] = {'id': proc_id, 'pid': item['pid'], 'host_start_time': item['host_start_time'],
            'profile': recorded, 'pid_scope': item['pid_scope'], 'source': 'process.registry',
            'observed_at': now, 'owners': owners}
        details[proc_id] = activity._detail(proc_id, item['command'], item['started_at'], item['pid'])
        for sid in owners:
            owned.setdefault(sid, set()).add(proc_id)
    for item in entries:
        if not isinstance(item, dict) or item.get('pid_scope', 'host') != 'host':
            continue
        if not activity._live(item.get('pid'), item.get('host_start_time')):
            continue
        pid = str(item.get('session_id') or item.get('pid'))
        owners = frozenset(activity._owner_ids(item))
        evidence.setdefault(pid, {'id': item.get('session_id') or None, 'pid': item.get('pid'),
            'host_start_time': item.get('host_start_time'), 'pid_scope': 'host', 'profile': profile,
            'source': 'process.checkpoint', 'observed_at': now, 'owners': owners})
        details.setdefault(pid, activity._detail(pid, item.get('command'), item.get('started_at'), item.get('pid')))
        for sid in owners:
            owned.setdefault(sid, set()).add(pid)
    return {'owned': owned, 'details': details, 'evidence': evidence,
            'gateway': gateway, 'local_profile': profile}, now + 30


def reviews(path, budget, *, ids, now):
    from hermes_cli import review_session_activity as review
    messages, keys = {}, set()
    if len(ids) > budget.max_keys:
        raise CaptureBudgetExceeded('review lineage exceeds capture key budget')
    size = 0
    with read_state(path, budget) as conn:
        for sid in sorted(ids):
            row = conn.execute("SELECT id,content FROM messages WHERE session_id=? AND role='assistant'"
                               " AND coalesce(content,'') <> '' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
            size += len(str(row[1]).encode('utf-8')) if row else 0
            if size > budget.max_bytes:
                raise CaptureBudgetExceeded('review assistant evidence exceeds total byte budget')
            message_keys = review.pr_keys(str(row[1])) if row else []
            messages[sid] = (row[0] if row else -1, message_keys)
            keys.update(message_keys)
    # Provider lookup is the retained asynchronous bounded canonical owner. Its
    # completion publication wakes our source; upstream TTL remains 300/120s.
    values = [(key, review._pr_info(key)) for key in sorted(keys)]
    due = [now + 30]
    for _, value in values:
        if value and value.get('requested_at') is not None:
            due.append(value['requested_at'] + review.PARK_WINDOW_S)
    return {'messages': messages, 'reviews': values}, min((d for d in due if d > now), default=now + 30)
