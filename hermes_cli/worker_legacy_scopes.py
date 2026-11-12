"""Explicit ownership records for legacy flat worker scopes.

Legacy jobs run in a flat child of the worker pool that carries no owner hash.
Such a populated child stays an *unknown* reservation unless an operator binds
it to (execution DB, task).  A record is bound to the live cgroup directory
(st_dev, st_ino) and the boot ID, so a recreated or post-reboot scope of the
same name never inherits the old owner; a stale record is simply ignored (the
scope is then unknown again).  A record that is unreadable or malformed for a
scope that currently exists raises, so admission fails closed.

Registration never moves, signals or otherwise touches the job.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from hermes_cli import worker_compute_capacity as capacity

BOOT_ID_PATH = Path('/proc/sys/kernel/random/boot_id')
MAX_CORES = 2
MAX_WEIGHT = 10
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@:-]{0,200}\Z')
_MANAGED = re.compile(r'hermes-kanban-[0-9a-f]{64}\.slice\Z')


def boot_id() -> str:
    value = BOOT_ID_PATH.read_text(encoding='utf-8-sig').strip()
    if not value:
        raise ValueError('empty boot id')
    return value


def valid_scope_name(name) -> bool:
    return (isinstance(name, str) and bool(_NAME.fullmatch(name)) and name not in {'.', '..'}
            and not _MANAGED.fullmatch(name))


def _directory() -> Path:
    return capacity.policy_root() / 'worker-resources' / 'legacy-owners'


def _record_path(name: str) -> Path:
    return _directory() / (hashlib.sha256(name.encode()).hexdigest() + '.json')


def _identity(path: Path) -> tuple[int, int]:
    st = path.lstat()  # never follow a symlink out of the pool
    import stat
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError('legacy scope is not a directory')
    return st.st_dev, st.st_ino


def _parse(raw, name: str) -> dict:
    keys = {'version', 'scope', 'owner', 'boot_id', 'dev', 'ino'}
    if (not isinstance(raw, dict) or set(raw) != keys
            or type(raw['version']) is not int or raw['version'] != 1 or raw['scope'] != name
            or not isinstance(raw['owner'], list) or len(raw['owner']) != 2
            or not all(isinstance(v, str) and v for v in raw['owner'])
            or not Path(raw['owner'][0]).is_absolute()
            or not isinstance(raw['boot_id'], str) or not raw['boot_id']
            or any(type(raw[k]) is not int or raw[k] < 0 for k in ('dev', 'ino'))):
        raise ValueError('invalid legacy worker owner record')
    return raw


def lookup(pool: Path, name: str):
    """Owner for a live flat scope, None if unregistered/stale. Corrupt raises."""
    if not valid_scope_name(name):
        return None
    try:
        text = _record_path(name).read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return None
    record = _parse(json.loads(text), name)
    try:
        dev, ino = _identity(Path(pool) / name)
    except FileNotFoundError:
        return None  # disappeared: stale record must not block anything
    if record['boot_id'] != boot_id() or (record['dev'], record['ino']) != (dev, ino):
        return None  # recreated or rebooted: do not alias the old owner
    return tuple(record['owner'])


def owners_for_pool(pool: Path) -> dict[str, tuple[str, str]]:
    """Map child name -> registered owner for populated-eligible flat children."""
    owners = {}
    try:
        children = list(Path(pool).iterdir())
    except FileNotFoundError:
        return owners
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        owner = lookup(pool, child.name)
        if owner is not None:
            owners[child.name] = owner
    return owners


def _populated(path: Path) -> bool:
    events = dict(line.split() for line in (path / 'cgroup.events').read_text(encoding='utf-8-sig').splitlines())
    if events.get('populated') not in {'0', '1'}:
        raise ValueError('invalid populated observation')
    return events['populated'] == '1'


def register(scope: str, database, task: str, *, pool: Path | None = None, timeout: float = 0) -> dict:
    """Bind a live populated legacy scope to (database, task) under the account fence."""
    from hermes_cli.admission_fence import SUPPORTED, admission_lock
    from hermes_constants import get_default_hermes_root

    if not SUPPORTED:
        raise ValueError('legacy scope registration requires admission fencing')
    if not valid_scope_name(scope):
        raise ValueError('invalid legacy scope name')
    owner = capacity.owner_for_db(database, task)
    if not Path(owner[0]).is_file():
        raise ValueError('execution database does not exist')
    pool = Path(capacity.pool_path() if pool is None else pool)
    account = capacity.policy_root().resolve()
    roots = list(dict.fromkeys([account, Path(get_default_hermes_root()).resolve()]))
    with contextlib.ExitStack() as stack:
        for root in roots:
            stack.enter_context(admission_lock(root, timeout=timeout))
        try:
            with contextlib.closing(sqlite3.connect(Path(owner[0]).as_uri() + '?mode=ro', uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone() is None:
                    raise ValueError('execution database has no tasks table')
                found = conn.execute('SELECT 1 FROM tasks WHERE id = ? COLLATE BINARY LIMIT 1', (task,)).fetchone()
        except sqlite3.Error as exc:
            raise ValueError('invalid execution database') from exc
        if found is None:
            raise ValueError('task does not exist in execution database')
        path = pool / scope
        if path.parent.resolve() != pool.resolve():
            raise ValueError('legacy scope escapes the worker pool')
        dev, ino = _identity(path)
        if not _populated(path):
            raise ValueError('legacy scope is not populated')
        capacity._verify_quota(path, MAX_CORES)
        if not 1 <= int((path / 'cpu.weight').read_text(encoding='utf-8-sig')) <= MAX_WEIGHT:
            raise ValueError('legacy scope CPU weight is missing or too high')
        record = {'version': 1, 'scope': scope, 'owner': list(owner), 'boot_id': boot_id(),
                  'dev': dev, 'ino': ino}
        existing = None
        with contextlib.suppress(FileNotFoundError):
            existing = _parse(json.loads(_record_path(scope).read_text(encoding='utf-8-sig')), scope)
        if existing is not None and existing != record and lookup(pool, scope) is not None:
            raise ValueError('legacy scope is already registered to another owner')
        if _identity(path) != (dev, ino):
            raise ValueError('legacy scope changed during registration')
        directory = _directory()
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if _identity(path) != (dev, ino):
                raise ValueError('legacy scope changed during registration')
            os.replace(temporary, _record_path(scope))
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
        return record


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog='worker_legacy_scopes')
    sub = parser.add_subparsers(dest='command', required=True)
    reg = sub.add_parser('register', help='bind a live legacy flat scope to a task')
    reg.add_argument('--scope', required=True)
    reg.add_argument('--database', required=True)
    reg.add_argument('--task', required=True)
    args = parser.parse_args(argv)
    try:
        record = register(args.scope, args.database, args.task)
    except Exception as exc:  # health: allow BLE001 -- CLI reports any refusal
        print(f'refused: {exc}')
        return 1
    print(json.dumps(record))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
