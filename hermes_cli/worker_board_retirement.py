"""Durable idle proofs for interrupted worker-board removal.

Only the fenced remover writes these proofs; every subsequent registration
invalidates them before a claim. Recovery never guesses from a missing path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from hermes_cli import worker_compute_capacity as capacity


def record_paths(path: Path) -> tuple[Path, Path]:
    key = hashlib.sha256(str(path).encode()).hexdigest() + '.json'
    root = capacity.policy_root() / 'worker-resources'
    return root / 'boards' / key, root / 'retiring' / key


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def missing(path: Path) -> bool:
    """Only FileNotFoundError (ENOENT) is absence; other OSErrors propagate.

    ``Path.exists()`` is version-dependent: Python 3.14+ returns False on any
    OSError, while 3.11-3.13 raise e.g. PermissionError. Inventory and
    retirement decisions must not depend on that: an unreadable path read as
    absent would drop or retire a board silently instead of refusing loudly.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return True
    return False


def discard(path: Path) -> None:
    """Make invalidation durable before allowing a new claim."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_directory(path.parent)


def prepare(path: Path) -> None:
    """Caller holds account/installation fences and has verified no running rows."""
    _, intent = record_paths(path)
    stat = path.stat()
    intent.parent.mkdir(parents=True, exist_ok=True)
    sync_directory(intent.parent.parent)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=intent.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump({'version': 1, 'path': str(path), 'device': stat.st_dev,
                   'inode': stat.st_ino, 'idle': True}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, intent)
        sync_directory(intent.parent)
    finally:
        temporary.unlink(missing_ok=True)


def pending(conn) -> Path | None:
    """Find a proof to invalidate even if resource policy was since disabled."""
    from hermes_cli.kanban_db_connect import _main_db_file
    raw = _main_db_file(conn)
    if not raw:
        return None
    _, intent = record_paths(Path(raw).resolve())
    return None if missing(intent) else intent


def recover(path: Path) -> None:
    """Explicit operator recovery of an interrupted, previously proven removal.

    Missing registrations without a durable idle proof require restoring the
    original database and draining/removing it normally; they are not dropped.
    """
    from hermes_cli.admission_fence import admission_lock
    path = path.resolve()
    root = capacity.policy_root().resolve()
    record, intent = record_paths(path)
    with admission_lock(root, timeout=0):
        if not missing(path):
            raise ValueError('source exists; use normal board removal')
        raw = json.loads(intent.read_text(encoding='utf-8-sig'))
        if (not isinstance(raw, dict) or raw.get('version') != 1
                or raw.get('path') != str(path) or raw.get('idle') is not True
                or type(raw.get('device')) is not int
                or type(raw.get('inode')) is not int or raw['inode'] <= 0):
            raise ValueError('invalid board retirement proof')
        if not missing(record):
            if json.loads(record.read_text(encoding='utf-8-sig')) != str(path):
                raise ValueError('registered identity differs from retirement proof')
            discard(record)
        discard(intent)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path, help='original absolute database path')
    args = parser.parse_args()
    if not args.database.is_absolute():
        parser.error('database must be an absolute original path')
    recover(args.database)
    print('Recovered proven-idle board retirement; detached jobs remain accounted.')


if __name__ == '__main__':
    main()
