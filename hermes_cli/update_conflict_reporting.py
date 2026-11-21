"""Opt-in, fail-closed routing for unattended updater conflict observations."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

CLOSED = frozenset({'done', 'failed', 'cancelled', 'archived'})
_REF = re.compile(r'@task:([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)/(t_[0-9a-f]{8}(?:[0-9a-f]{8})?)\Z')


def incident_key(cwd: Path) -> str:
    return 'upstream-merge-conflict:' + hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:24]


def parse(value: object) -> dict | None:
    """None is the registered legacy default; every explicit mapping must be complete."""
    if value is None:
        return None
    prefix = 'update.upstream_conflict_reporting'
    if not isinstance(value, dict) or set(value) != {'mode', 'owner', 'repository', 'owner_checkout'}:
        raise ValueError(f'{prefix} needs mode, owner, repository and owner_checkout')
    if value['mode'] not in ('inventory', 'pointer'):
        raise ValueError(f'{prefix}.mode must be inventory or pointer')
    owner = value['owner']
    match = _REF.fullmatch(owner) if isinstance(owner, str) else None
    if not match:
        raise ValueError(f'{prefix}.owner must be @task:board@gateway/t_<8 or 16 lowercase hex>')
    path, repo = value['owner_checkout'], value['repository']
    if not isinstance(path, str) or not path.startswith('/') or '`' in path or '\n' in path:
        raise ValueError(f'{prefix}.owner_checkout must be an absolute owner-host checkout path')
    if not isinstance(repo, str) or not re.fullmatch(r'https://[^/\s@]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', repo):
        raise ValueError(f'{prefix}.repository must be a canonical HTTPS repository URL ending in .git')
    return {**value, 'board': match[1], 'gateway': match[2], 'task_id': match[3]}


def configured() -> dict | None:
    from hermes_cli.config_effective import load_user_config_effective
    update = load_user_config_effective(fail_closed=True).get('update', {})
    if not isinstance(update, dict):
        raise ValueError('update must be a mapping')
    return parse(update.get('upstream_conflict_reporting'))


def _repository(cwd: Path) -> str:
    """Read only; normalize equivalent SSH/HTTPS origin spellings without executing repository code."""
    proc = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=cwd, check=True,
                          capture_output=True, text=True, encoding='utf-8', timeout=10)
    url = proc.stdout.strip()
    match = re.fullmatch(r'(?:git@([^:]+):|https://([^/]+)/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', url)
    if not match:
        raise ValueError('upstream conflict reporting: origin is not a supported repository identity')
    return f'https://{match[1] or match[2]}/{match[3]}.git'


def _owner_evidence(board: str, task_id: str) -> dict:
    from hermes_cli.kanban_sync_client import Hub, load_identity
    hub = Hub(load_identity(), timeout=10)
    try:
        return {**hub.row_evidence(board, 'tasks', task_id), 'reporting_gateway': hub.ident.gateway}
    finally:
        hub.close()


def validate_owner(cfg: dict, cwd: Path) -> str:
    """An administrator binds this repository to an existing, gateway-qualified incident owner."""
    if _repository(cwd) != cfg['repository']:
        raise ValueError('upstream conflict reporting: reporting repository does not match configured repository')
    evidence = _owner_evidence(cfg['board'], cfg['task_id'])
    if (not isinstance(evidence, dict)
            or (evidence.get('board'), evidence.get('table'), evidence.get('uid')) != (cfg['board'], 'tasks', cfg['task_id'])
            or evidence.get('state') != 'live' or evidence.get('tombstone') is not None
            or type(evidence.get('head')) is not int or not isinstance(evidence.get('row'), dict)):
        raise ValueError('upstream conflict reporting: authoritative owner unavailable or invalid')
    row = evidence['row']
    # Hash the owner's already-resolved path as text: resolving a foreign host's path locally is wrong.
    key = 'upstream-merge-conflict:' + hashlib.sha256(cfg['owner_checkout'].encode()).hexdigest()[:24]
    lead = f"`hermes update` could not merge upstream into `{cfg['owner_checkout']}`. Nothing moved.\n"
    if (row.get('uid') != cfg['task_id'] or row.get('created_on') != cfg['gateway']
            or row.get('deleted_at') is not None or type(row.get('version')) is not int or row['version'] <= 0
            or row.get('status') not in {'todo', 'ready', 'running', 'review', 'blocked', 'triage', 'scheduled'}
            or row.get('created_by') != 'upstream-merge' or row.get('title') != 'Resolve unattended upstream merge conflict'
            or row.get('idempotency_key') != key or not isinstance(row.get('body'), str) or not row['body'].startswith(lead)):
        raise ValueError('upstream conflict reporting: owner identity, project binding or status does not match')
    gateway = evidence.get('reporting_gateway')
    if not isinstance(gateway, str) or not gateway:
        raise ValueError('upstream conflict reporting: authenticated reporting gateway unavailable')
    if cfg['mode'] == 'inventory' and (str(cwd.resolve()) != cfg['owner_checkout'] or gateway != cfg['gateway']):
        raise ValueError('upstream conflict reporting: inventory is restricted to the designated owner checkout and gateway')
    return gateway


def report(conn, cfg: dict, cwd: Path, upstream: str, conflicts: list[str]) -> str:
    """Only add observations; never create/reopen/redirect tasks or change acceptance/holds."""
    from hermes_cli import kanban_db as kb
    gateway = validate_owner(cfg, cwd)  # Network outside the local transaction; never trust a stale replica.
    key = incident_key(cwd)
    with kb.write_txn(conn, allow_nested=True):
        if cfg['mode'] == 'inventory':
            from hermes_cli.kanban_db_sync import board_slug_for_path
            path = conn.execute('PRAGMA database_list').fetchone()[2]
            if board_slug_for_path(path) != cfg['board']:
                raise ValueError('upstream conflict reporting: inventory board does not match configured owner')
            row = conn.execute('SELECT id,status,body,idempotency_key,created_by,title FROM tasks WHERE id=?',
                               (cfg['task_id'],)).fetchone()
            if (not row or row[1] in CLOSED or row[3] != key or row[4] != 'upstream-merge'
                    or row[5] != 'Resolve unattended upstream merge conflict'):
                raise ValueError('upstream conflict reporting: inventory owner is not an eligible local incident')
            notes = [(row[0], f'Upstream SHA: `{upstream}`\nConflicting paths:\n' + '\n'.join(f'- `{p}`' for p in conflicts), row[2])]
        else:
            pointer = f"Upstream conflict `{upstream}`; integration owner: {cfg['owner']}. Incident: {key}. No local integration authorized."
            columns = {r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
            origin = 'created_on' if 'created_on' in columns else 'NULL'
            rows = conn.execute(f'SELECT id,status,body,{origin} FROM tasks WHERE idempotency_key=?', (key,)).fetchall()
            notes = [(r[0], pointer, r[2]) for r in rows
                     if r[1] not in CLOSED and r[0] != cfg['task_id'] and r[3] in (None, '', gateway)]
        for task_id, text, body in notes:
            if text not in (body or '') and not conn.execute(
                    'SELECT 1 FROM task_comments WHERE task_id=? AND author=? AND body=?',
                    (task_id, 'upstream-merge', text)).fetchone():
                kb.add_comment(conn, task_id, 'upstream-merge', text)
    return notes[0][0] if notes else cfg['task_id']
