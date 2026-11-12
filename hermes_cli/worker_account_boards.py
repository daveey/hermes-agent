"""Account-wide inventory of execution DBs admitted by worker installations.

Registration happens under account admission.lock before the final gate. Records
survive process exits: a parked dispatcher is not evidence its running rows or
jobs vanished. Missing registered DBs are unknown, never silently free capacity.
"""
from __future__ import annotations

import contextlib
import hashlib
import sqlite3
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
    from hermes_cli.worker_board_retirement import record_paths, discard
    # A fresh admission invalidates any interrupted pre-rename idle proof.
    # The caller retains the account fence through the subsequent claim.
    _, intent = record_paths(path)
    discard(intent)
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


@contextlib.contextmanager
def removing(path: Path):
    """Fence board removal against claims; retire only a proven idle database.

    Detached jobs remain counted by their cgroups, independently of this record.
    A failed removal keeps the registration. A missing DB is never proof of idle.
    The caller already owns the board dispatch lock; admission locks are taken
    nonblocking, matching claim_transaction's deadlock-avoidance contract.
    """
    from hermes_constants import get_default_hermes_root
    from hermes_cli.admission_fence import AdmissionBusy, SUPPORTED, admission_lock

    path = path.resolve()
    account = capacity.policy_root().resolve()
    record = account / 'worker-resources' / 'boards' / (
        hashlib.sha256(str(path).encode()).hexdigest() + '.json')
    if not SUPPORTED:
        if capacity.worker_enabled() or record.exists():
            raise ValueError('worker board removal requires admission fencing')
        yield
        return
    roots = list(dict.fromkeys([account, Path(get_default_hermes_root()).resolve()]))
    with contextlib.ExitStack() as stack:
        try:
            for root in roots:
                stack.enter_context(admission_lock(root, timeout=0))
            registered = record.exists()
            if registered and json.loads(record.read_text(encoding='utf-8')) != str(path):
                raise ValueError('invalid registered board')
            verify = registered or (capacity.worker_enabled() and path.exists())
            if verify:
                conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)
                try:
                    # Refuse an in-flight writer, not its uncommitted row.
                    conn.execute('BEGIN IMMEDIATE')
                    if conn.execute("SELECT 1 FROM tasks WHERE status = 'running' LIMIT 1").fetchone():
                        raise ValueError('worker board has running cards; drain before removal')
                finally:
                    conn.rollback()
                    conn.close()
        except (AdmissionBusy, sqlite3.Error) as exc:
            raise ValueError(f'board busy or unreadable; retry removal: {exc}') from exc
        # Claims cannot pass their final gate until after removal and retirement.
        from hermes_cli import worker_board_retirement as retirement
        _, intent = retirement.record_paths(path)
        if verify:
            retirement.prepare(path)
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            # Keep the durable proof if record retirement is interrupted after
            # rename. A later explicit recovery must take the account fence.
            if succeeded or (verify and not path.exists()):
                retirement.discard(record)
            retirement.discard(intent)


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
