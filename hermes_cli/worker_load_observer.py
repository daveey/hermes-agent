"""Independent 30-second sustained worker-load observer with durable alert outbox.

Run as a system timer outside hermes-kanban.slice, never from admission. No
process argv, environment, paths or credentials are included in alert evidence.
"""
from __future__ import annotations

import json
from contextlib import closing
import math
import os
from pathlib import Path
import tempfile
import uuid

from hermes_cli.worker_board_retirement import sync_directory


def _save(directory: Path, state: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / 'observer.json')
        sync_directory(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def tick(directory: Path, *, now: float, load_per_core: float | None, evidence: dict, publish) -> dict:
    """Persist observation/outbox before publication; retries keep the exact key.

    Gaps >90 seconds, reversed clocks and unreadable samples explicitly break
    continuity as unknown. They are not observations of recovered load.
    """
    import fcntl
    if not math.isfinite(now):
        raise ValueError('invalid observer clock')
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'observer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            state = json.loads((directory / 'observer.json').read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            state = dict(version=1, last=None, high_since=None, pending=None, published_id=None, armed=True)
        if not isinstance(state, dict) or state.get('version') != 1:
            raise ValueError('invalid observer state')
        previous = state['last']
        gap = previous is not None and (now <= previous or now - previous > 90)
        valid = load_per_core is not None and math.isfinite(load_per_core) and load_per_core >= 0
        state['coverage'] = 'unknown_sample' if not valid else 'unknown_gap' if gap else 'observed'
        if gap or not valid:
            state['high_since'] = None
            state['last_unknown'] = now
        if valid:
            if load_per_core <= 2:
                state['high_since'] = None
                state['armed'] = True
            elif state['high_since'] is None:
                state['high_since'] = now
        state.update(last=now, load_per_core=load_per_core if valid else None)
        since = state['high_since']
        if (valid and since is not None and now - since >= 600
                and state['armed'] and state['pending'] is None):
            state['pending'] = dict(key='worker-load:' + uuid.uuid4().hex,
                                    since=since, observed_at=now, load_per_core=load_per_core,
                                    evidence=evidence)
            state['armed'] = False
        _save(directory, state)
        if state['pending'] is not None:
            try:
                task = publish(state['pending'])
                if not isinstance(task, str) or not task:
                    raise ValueError('publisher returned no card identity')
            except Exception as exc:  # health: allow BLE001 -- retain durable outbox across publisher failures
                state['publish_error'] = type(exc).__name__
            else:
                state.update(pending=None, published_id=task, publish_error=None)
            _save(directory, state)
        return state


def publish_alert(alert: dict) -> str:
    """Create/read back one durable alert card, including after uncertain success."""
    import socket
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.worker_compute_capacity import policy_root
    # This independent service has no worker DB/profile environment. Explicit
    # file path avoids a current-board symlink changing alert custody.
    with closing(connect(db_path=policy_root() / 'kanban.db')) as conn:
        # Include archived cards: a crash after create and operator archive must
        # not recreate a second card for the same incident on retry.
        existing = conn.execute('SELECT id FROM tasks WHERE idempotency_key=?', (alert['key'],)).fetchone()
        if existing:
            return existing[0]
        body = ('Measured load/core stayed above 2 for at least 600 seconds with no sampling gap over 90 seconds. '
                'Investigate owning cards and unexpected scopes; do not kill workloads without authorization.\n\n'
                + json.dumps(alert, indent=2, sort_keys=True))
        task = kb.create_task(conn, title=f'Worker sustained load: {socket.gethostname()}', body=body,
                              assignee='default', created_by='worker-load-observer', requires=['terminal'],
                              workspace_kind='scratch', idempotency_key=alert['key'], initial_status='running',
                              completion_contract='local-only')
        stored = conn.execute('SELECT id, idempotency_key FROM tasks WHERE id=?', (task,)).fetchone()
        if not stored or stored[1] != alert['key']:
            raise ValueError('alert readback failed')
        return task


def main() -> None:
    import time
    from hermes_cli import worker_compute_capacity as capacity
    from hermes_cli.worker_load_snapshot import snapshot
    if not capacity.worker_enabled():
        return
    directory = capacity.policy_root() / 'worker-resources' / 'observer'
    try:
        cores = capacity.effective_cores()
        ratio = os.getloadavg()[0] / cores
    except (OSError, ValueError):
        ratio = None
    try:
        evidence = snapshot()
    except Exception:  # health: allow BLE001 -- auxiliary evidence must never suppress load observation
        evidence = {'process_inventory': 'unknown'}
    tick(directory, now=time.time(), load_per_core=ratio, evidence=evidence, publish=publish_alert)


if __name__ == '__main__':
    main()
