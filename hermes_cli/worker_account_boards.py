"""Account-wide inventory of execution DBs admitted by worker installations.

Registration happens under account admission.lock before the final gate. Records
survive process exits: a parked dispatcher is not evidence its running rows or
jobs vanished. Missing registered DBs are unknown, never silently free capacity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import tempfile

from hermes_cli import worker_compute_capacity as capacity


def register(conn) -> None:
    from hermes_cli.kanban_db_connect import _main_db_file
    raw = _main_db_file(conn)
    if not raw:
        raise ValueError('worker admission requires a file-backed board')
    path = Path(raw).resolve(strict=True)
    directory = capacity.policy_root() / 'worker-resources' / 'boards'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (hashlib.sha256(str(path).encode()).hexdigest() + '.json')
    if destination.exists():
        if json.loads(destination.read_text(encoding='utf-8')) != str(path):
            raise ValueError('invalid registered board')
        return
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(str(path), handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def paths() -> list[Path]:
    root = capacity.policy_root()
    found = []
    # Account installation boards count even before their first new claim.
    default = root / 'kanban.db'
    if default.exists():
        found.append(default)
    boards = root / 'kanban' / 'boards'
    if boards.exists():
        from hermes_cli import kanban_db as kb
        found.extend(child / 'kanban.db' for child in boards.iterdir()
                     if kb._BOARD_SLUG_RE.match(child.name) and (child / 'kanban.db').is_file())
    directory = root / 'worker-resources' / 'boards'
    if directory.exists():
        for record in directory.iterdir():
            if record.suffix != '.json':
                continue
            raw = json.loads(record.read_text(encoding='utf-8'))
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise ValueError('invalid registered board path')
            if record.stem != hashlib.sha256(raw.encode()).hexdigest():
                raise ValueError('invalid registered board identity')
            Path(raw).stat()  # disappearance is unknown, including default boards
            found.append(Path(raw))
    return found
