"""Account-wide inventory of execution DBs admitted by worker installations.

Registration happens under account admission.lock before the final gate. Records
survive process exits: a parked dispatcher is not evidence its running rows or
jobs vanished. Missing registered DBs are unknown, never silently free capacity,
except a board whose whole directory was removed from an ephemeral root (system
temp, /dev/shm, the account scratch dir): that workspace was garbage-collected, so its
record retires instead of holding every claim on the host forever.

Reads (``paths()``, called by ``refusal()`` and the never-mutating
``occupied_count_for_host`` census) only skip a collected record, or a record a
fenced writer unlinked after the listing. Its file is
unlinked by ``register()``, which runs under the account admission fence: an
unfenced unlink could delete the record a concurrent ``register()`` of the same
recreated scratch path had just found and kept, losing that board until its next
claim.

Accepted trade-off: if the pruner deletes a scratch board's directory while its
process still has the DB open, the record retires and that board's running rows
stop counting toward host capacity. Its detached jobs still count through their
cgroups; only the rows of a board whose workspace is already gone are dropped.
"""
from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import json
from pathlib import Path
import os
import stat
import tempfile

from hermes_cli import worker_compute_capacity as capacity
from hermes_cli import worker_board_retirement as retirement
from hermes_cli.worker_board_retirement import missing as _missing


def _refuse_test_registration() -> None:
    """A pytest process never writes the real account inventory.

    conftest isolates ``policy_root``; a test's ``monkeypatch.undo()`` also drops
    that patch, and a later claim then registered its tmp board in the real
    account. Once pytest deleted the board, every worker claim on the host was
    deferred ``worker_resources_unknown``. Compare paths without I/O: the
    test-only real-home guard refuses any stat under the real root.

    The test signal is env OR ancestry (``hermes_state_guard``): a child that
    rebuilt its environment loses ``PYTEST_*`` but keeps our inherited
    ``HERMES_TEST_ISOLATION`` marker and its pytest ancestor.
    """
    import hermes_state_guard
    if not hermes_state_guard._in_test_context():
        return
    from hermes_constants import _account_home
    real = os.path.normpath(str(_account_home() / '.hermes'))
    if os.path.normpath(str(capacity.policy_root())) == real:
        raise RuntimeError('pytest must not register boards in the real account worker '
                           f'inventory ({real}); is worker_compute_capacity.policy_root isolated?')


# RAM-backed, boot-cleared temp roots. No real board lives there: state's run-state-suite-local runs its shards
# with TMPDIR on /dev/shm, and a shard board left registered there held every claim on worker-1 for 17 min
# (2026-11-13, t_01a1d5bf526e1344).
TMPFS_ROOTS: tuple[Path, ...] = (Path('/dev/shm'),)


def _ephemeral_roots() -> list[Path]:
    account = capacity.policy_root().resolve()
    # no-tmp: ok — matches boards OTHER processes made in system temp (pytest tmp_path), not our own scratch
    roots = {Path('/tmp'), Path('/var/tmp'), Path(tempfile.gettempdir()), account / 'cache' / 'scratch',
             *TMPFS_ROOTS}
    # An odd TMPDIR (a home or /) must never make the account's own boards ephemeral.
    return [root for root in (r.resolve() for r in roots) if not account.is_relative_to(root)]


def _collected(path: Path) -> bool:
    """The board's directory is gone from a temp/scratch root: GC'd, not unknown.

    A real board (installation root, any non-ephemeral path) or a missing file in
    a surviving directory stays unknown and keeps refusing claims.
    """
    if not _missing(path.parent):
        return False
    return any(path.is_relative_to(root) for root in _ephemeral_roots())


def _retire_collected(directory: Path) -> None:
    """Unlink records of garbage-collected boards. Caller holds the account fence.

    Malformed records are left for ``paths()`` to refuse loudly.
    """
    for record in directory.iterdir():
        if record.suffix != '.json':
            continue
        try:
            raw = json.loads(record.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        if (not isinstance(raw, str) or not Path(raw).is_absolute()
                or record.stem != hashlib.sha256(raw.encode()).hexdigest()):
            continue
        try:
            gone = _missing(Path(raw)) and _collected(Path(raw))
        except OSError:
            continue  # unreadable is not missing: paths() refuses it loudly
        if gone:
            record.unlink(missing_ok=True)


def _account_board(path: Path) -> bool:
    """Only canonical account boards may enter the real host inventory.

    Explicit isolated test policy roots retain their multi-installation fixtures.
    HOME/HERMES_HOME never authorize a new real-account board location.
    """
    from hermes_constants import _account_home
    root = capacity.policy_root().resolve()
    if root != (_account_home() / '.hermes').resolve():
        return True
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        return False
    parts = resolved.relative_to(root).parts
    if len(parts) >= 3 and parts[0] == 'profiles':
        parts = parts[2:]
    from hermes_cli import kanban_db as kb
    return parts == ('kanban.db',) or (
        len(parts) == 4 and parts[:2] == ('kanban', 'boards')
        and kb._BOARD_SLUG_RE.fullmatch(parts[2]) is not None
        and parts[3] == 'kanban.db')


def register(conn) -> None:
    """Record *conn*'s board. Caller holds the account admission fence."""
    _refuse_test_registration()
    from hermes_cli.kanban_db_connect import _main_db_file
    raw = _main_db_file(conn)
    if not raw:
        raise ValueError('worker admission requires a file-backed board')
    path = Path(raw).resolve(strict=True)
    if not _account_board(path):
        return
    # A fresh admission invalidates any interrupted pre-rename idle proof.
    # The caller retains the account fence through the subsequent claim.
    _, intent = retirement.record_paths(path)
    retirement.discard(intent)
    directory = capacity.policy_root() / 'worker-resources' / 'boards'
    directory.mkdir(parents=True, exist_ok=True)
    _retire_collected(directory)
    destination = directory / (hashlib.sha256(str(path).encode()).hexdigest() + '.json')
    if not _missing(destination):
        if json.loads(destination.read_text(encoding='utf-8-sig')) != str(path):
            raise ValueError('invalid registered board')
        return
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(str(path), handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, destination)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
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
        if capacity.worker_enabled() or not _missing(record):
            raise ValueError('worker board removal requires admission fencing')
        yield
        return
    roots = list(dict.fromkeys([account, Path(get_default_hermes_root()).resolve()]))
    with contextlib.ExitStack() as stack:
        try:
            for root in roots:
                stack.enter_context(admission_lock(root, timeout=0))
            registered = not _missing(record)
            if registered and json.loads(record.read_text(encoding='utf-8-sig')) != str(path):
                raise ValueError('invalid registered board')
            verify = registered or (capacity.worker_enabled() and not _missing(path))
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
            try:
                removed = verify and _missing(path)
            except OSError:
                removed = False  # unreadable is not removed: keep the record
            if succeeded or removed:
                retirement.discard(record)
            retirement.discard(intent)


def _board_file(path: Path) -> bool:
    """A named board's DB file; none (or a non-directory slug entry) is no board."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _installation_boards(root: Path) -> list[Path]:
    found = []
    # Only ENOENT is absence (``_missing``): an unreadable directory raises
    # instead of silently dropping every board under it.
    # Account installation boards count even before their first new claim.
    default = root / 'kanban.db'
    if not _missing(default):
        found.append(default)
    boards = root / 'kanban' / 'boards'
    if not _missing(boards):
        from hermes_cli import kanban_db as kb
        found.extend(child / 'kanban.db' for child in boards.iterdir()
                     if kb._BOARD_SLUG_RE.match(child.name) and _board_file(child / 'kanban.db'))
    return found


def paths() -> list[Path]:
    root = capacity.policy_root()
    found = _installation_boards(root)
    profiles = root / 'profiles'
    if not _missing(profiles):
        for profile in profiles.iterdir():
            try:
                is_directory = stat.S_ISDIR(profile.stat().st_mode)
            except FileNotFoundError:
                continue  # entry vanished after listing; other I/O errors are unknown
            if is_directory:
                found.extend(_installation_boards(profile))
    directory = root / 'worker-resources' / 'boards'
    if not _missing(directory):
        for record in directory.iterdir():
            if record.suffix != '.json':
                continue
            try:
                raw = json.loads(record.read_text(encoding='utf-8-sig'))
            except FileNotFoundError:
                # A fenced register()/removing() unlinked it after iterdir():
                # the record is gone, not its board. A vanished board is below.
                continue
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise ValueError('invalid registered board path')
            if record.stem != hashlib.sha256(raw.encode()).hexdigest():
                raise ValueError('invalid registered board identity')
            if not _account_board(Path(raw)):
                continue
            try:
                Path(raw).stat()  # disappearance is unknown, including default boards
            except FileNotFoundError:
                if not _collected(Path(raw)):
                    raise
                continue  # read-only: register() retires it under the fence
            found.append(Path(raw))
    return list(dict.fromkeys(path for path in found if _account_board(path)))
