"""Host-shared, durable FD incident coalescing through the existing hub courier."""
from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)
HOUR = 3600


def _path(probe: bool) -> Path:
    return get_default_hermes_root() / 'runtime' / ('fd-headroom-probe.json' if probe else 'fd-headroom.json')


def _run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=12, check=False)
    if result.returncode not in (0, 3):
        raise OSError(f'courier exit {result.returncode}: {result.stderr[-300:]}')
    return result.stdout


def _courier():
    from hermes_cli import kanban_capability_registry as registry, kanban_placement as placement
    return registry.hub_cli_path(placement.config().get('hub_cli'))


def _rows(cli, run):
    rows = json.loads(run([cli, 'tasks', '--gw', 'hub', '--json', '--all']))
    if not isinstance(rows, list):
        raise ValueError('invalid hub card inventory')
    return rows


def _pending_task(request_id):
    from hermes_cli import kanban_placement as placement
    return placement.remote_task_id(request_id), placement.remote_state(request_id)


def _load(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    temporary.replace(path)


def report(sample: dict, *, probe=False, now=None, run=None, cli=None) -> dict:
    """Serialize across server processes/profiles, reserve before sending, and back off errors.

    An uncertain create keeps the SAME frozen request; it is never treated as an unreadable
    card. Completed probe incidents retain their one-hour suppression even after archiving.
    """
    import fcntl
    from hermes_state_guard import _in_test_context
    if run is None and _in_test_context():
        return {'error': 'test process: courier disabled'}
    path = _path(probe)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _load(path)
        state = _load(path)
        wall = time.time() if now is None else now
        if sample.get('raised'):
            state['raises'] = (state.get('raises', []) + [sample['raised']])[-8:]
        if wall < state.get('next_attempt', 0):
            _save(path, state)
            return state
        try:
            _report(sample, state, wall, probe, run or _run, cli or _courier(), path)
        except Exception as exc:  # health: allow BLE001 -- a failed alert is visible, never fatal to the tick
            state.setdefault('failure_since', wall)
            state.update(error=str(exc)[:500], next_attempt=wall + HOUR)
            logger.warning('fd-headroom: card create/report failed: %s', state['error'])
        _save(path, state)
        return state


def _report(sample, state, now, probe, run, cli, path):
    rows = _rows(cli, run)
    tid = state.get('task_id')
    pending = state.get('pending')
    if pending:
        found = next((r for r in rows if r.get('idempotency_key') == pending['id']), None)
        if found:
            tid = found['id']
        else:
            tid, status = _pending_task(pending['id'])
            if not tid:
                if status in {'rejected', 'failed', 'expired'}:
                    state.pop('pending', None)
                    raise OSError(f'card create rejected: {status}')
                # Exact immutable replay only, after the hourly backoff (including lost acknowledgements).
                if now >= pending['retry_at']:
                    pending['retry_at'] = now + HOUR
                    _save(path, state)
                    run(pending['argv'])
                return
        state.update(task_id=tid)
        state.pop('pending', None)
    row = next((r for r in rows if r.get('id') == tid), None)
    opened = tid and row and row.get('status') not in {'done', 'archived'}
    text = json.dumps({**sample, 'raises': state.get('raises', [])}, sort_keys=True)
    if opened:
        if not sample.get('triggered', True) and not state.get('raises'):
            return
        if now - state.get('commented_at', state.get('filed_at', 0)) < HOUR:
            return
        state.update(commented_at=now)
        _save(path, state)  # reserve the hour even when the acknowledgement is lost
        run([cli, 'request', 'hub', 'tasks', f'default/default/{tid}', 'comment',
             '--arg', f'text=fd-headroom: {text}', '--id', f'fd-headroom:{tid}:{int(now)}'])
        state['raises'] = []
    else:
        if not sample.get('triggered', True):
            return
        if probe and now - state.get('filed_at', -HOUR) < HOUR:
            return
        key = f'fd-headroom:{sample["host"]}:{uuid.uuid4().hex}'
        top = max(sample['census'], key=sample['census'].get, default='other')
        title = (f'fd headroom: {sample["host"]} {sample["role"]} at '
                 f'{sample["count"]}/{sample["soft"]} fds ({top})')
        body = (f'Process FD headroom warning.\n\n{ text }\n\n'
                'Investigate descriptor growth and the installed core. Never signal/restart production '
                'services, change configured nofile limits or raise a hard limit for this alert. '
                'The guard may have raised only its own soft limit; see raised above.')
        argv = [cli, 'create-task', 'hub', ('[probe] ' if probe else '') + title,
                '--body', body, '--assignee', 'default', '--board', 'default', '--origin-session', '',
                '--id', key, '--option', f'idempotency_key={json.dumps(key)}',
                '--option', 'runs_on=["pool:worker"]']
        if probe:
            argv += ['--option', 'initial_status="blocked"']
        state.update(pending={'id': key, 'argv': argv, 'retry_at': now + HOUR}, task_id=None,
                     filed_at=now, commented_at=now)
        _save(path, state)  # persist frozen envelope BEFORE any possible external write
        run(argv)
    state.pop('error', None)
    state.pop('failure_since', None)


def failure_note(state: dict, now=None) -> str:
    since = state.get('failure_since')
    if since is not None and (time.time() if now is None else now) - since >= HOUR:
        stamp = datetime.fromtimestamp(since, UTC).isoformat()
        return f'card create failing since {stamp}: {state.get("error", "unknown")}'
    return ''
