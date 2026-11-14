"""Registered installation boards remain visible across dispatcher roots."""
import errno
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import hermes_constants
import hermes_state_guard
from hermes_cli import worker_account_boards as inventory
from hermes_cli import worker_board_retirement
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli import machine_metrics, kanban_db as kb


def test_registered_foreign_board_counts_after_installation_changes(monkeypatch, tmp_path):
    account = tmp_path / 'account'
    account.mkdir()
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    foreign = tmp_path / 'foreign' / 'kanban.db'
    foreign.parent.mkdir()
    conn = sqlite3.connect(foreign)
    conn.execute('CREATE TABLE tasks (id TEXT, status TEXT)')
    conn.execute("INSERT INTO tasks VALUES ('t_foreign', 'running')")
    conn.commit()
    try:
        inventory.register(conn)
        inventory.register(conn)
        monkeypatch.setattr(kb, 'kanban_home', lambda: account)
        monkeypatch.setattr('hermes_cli.kanban_db_boards.named_board_db_files', list)
        owners = set()
        assert machine_metrics.active_card_runs(extra_paths=inventory.paths(), owner_output=owners) == 1
        assert owners == {(str(foreign.resolve()), 't_foreign')}
        assert machine_metrics.active_card_runs(extra_paths=[foreign, foreign]) == 1
    finally:
        conn.close()
    foreign.unlink()
    with pytest.raises(FileNotFoundError):
        inventory.paths()


@pytest.mark.parametrize('raw', ['{broken', '7', '"relative.db"'])
def test_corrupt_inventory_fails_closed(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    records = tmp_path / 'worker-resources' / 'boards'
    records.mkdir(parents=True)
    (records / 'bad.json').write_text(raw)
    with pytest.raises(ValueError):
        inventory.paths()


def test_register_directory_fsync_failure_propagates(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    real_fsync = os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory fsync failed')
        real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    conn = sqlite3.connect(tmp_path / 'board.db')
    try:
        with pytest.raises(OSError, match='directory fsync failed'):
            inventory.register(conn)
    finally:
        conn.close()


def test_unregistered_account_boards_count(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    default = tmp_path / 'kanban.db'
    default.touch()
    named = tmp_path / 'kanban' / 'boards' / 'other' / 'kanban.db'
    named.parent.mkdir(parents=True)
    named.touch()
    assert set(inventory.paths()) == {default, named}


def _registered(conn_path):
    conn = sqlite3.connect(conn_path)
    try:
        inventory.register(conn)
    finally:
        conn.close()


def test_garbage_collected_scratch_board_retires_its_record(monkeypatch, tmp_path):
    """A scratch HERMES_HOME's board whose workspace was pruned no longer holds every claim."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    workspace = tmp_path / 'cache' / 'scratch' / 't_bench-x' / 'home'
    workspace.mkdir(parents=True)
    _registered(workspace / 'kanban.db')
    records = list((tmp_path / 'worker-resources' / 'boards').glob('*.json'))
    assert len(records) == 1 and inventory.paths() == [(workspace / 'kanban.db').resolve()]
    shutil.rmtree(tmp_path / 'cache' / 'scratch' / 't_bench-x')
    assert inventory.paths() == []
    assert records[0].exists(), 'reads never unlink outside the account fence'
    other = tmp_path / 'cache' / 'scratch' / 't_other' / 'kanban.db'
    other.parent.mkdir(parents=True)
    _registered(other)  # the fenced mutating path retires the collected record
    assert not records[0].exists()
    assert inventory.paths() == [other.resolve()]


def test_concurrent_register_of_recreated_scratch_board_keeps_its_record(monkeypatch, tmp_path):
    """paths() saw the board collected; a register() of the recreated path then
    kept the existing record. The read must not unlink it afterwards."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    board = tmp_path / 'cache' / 'scratch' / 'run' / 'kanban.db'
    board.parent.mkdir(parents=True)
    _registered(board)
    record, = (tmp_path / 'worker-resources' / 'boards').glob('*.json')
    shutil.rmtree(board.parent)
    real_collected = inventory._collected

    def collected_then_concurrent_register(path):
        verdict = real_collected(path)
        assert verdict
        board.parent.mkdir(parents=True)  # the scratch path is recreated ...
        before = record.stat().st_mtime_ns
        _registered(board)  # ... and registered: record exists, early return
        assert record.stat().st_mtime_ns == before
        return verdict

    monkeypatch.setattr(inventory, '_collected', collected_then_concurrent_register)
    assert inventory.paths() == []
    monkeypatch.setattr(inventory, '_collected', real_collected)
    assert record.exists()
    assert inventory.paths() == [board.resolve()]


def test_missing_board_stays_unknown_unless_its_scratch_dir_is_gone(monkeypatch, tmp_path):
    """Fail closed for real boards: a non-ephemeral root, or a surviving scratch dir."""
    account = tmp_path / 'account'
    account.mkdir()
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    real = tmp_path / 'other-install' / 'kanban.db'
    real.parent.mkdir()
    _registered(real)
    shutil.rmtree(real.parent)
    with pytest.raises(FileNotFoundError):
        inventory.paths()
    shutil.rmtree(account / 'worker-resources')
    scratch = account / 'cache' / 'scratch' / 'run' / 'kanban.db'
    scratch.parent.mkdir(parents=True)
    _registered(scratch)
    scratch.unlink()  # the directory survives: a replaced or moving board, not a pruned workspace
    with pytest.raises(FileNotFoundError):
        inventory.paths()


def test_record_unlinked_between_listing_and_read_is_skipped(monkeypatch, tmp_path):
    """paths() runs unfenced: a fenced register()/removing() may unlink a record
    after iterdir() listed it. That record is gone, not a reason to fail the census."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    kept = tmp_path / 'kept' / 'kanban.db'
    gone = tmp_path / 'gone' / 'kanban.db'
    for board in (kept, gone):
        board.parent.mkdir()
        _registered(board)
    victim = worker_board_retirement.record_paths(gone.resolve())[0]
    assert victim.exists()
    real_read_text = Path.read_text

    def unlink_then_read(self, *args, **kwargs):
        if self == victim:
            self.unlink()  # the concurrent fenced writer wins the race
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', unlink_then_read)
    assert inventory.paths() == [kept.resolve()]
    # A vanished *board* (not its record) still fails closed as before.
    monkeypatch.setattr(Path, 'read_text', real_read_text)
    kept.unlink()
    with pytest.raises(FileNotFoundError):
        inventory.paths()


def test_unreadable_collected_looking_board_is_not_retired(monkeypatch, tmp_path):
    """_retire_collected and paths() agree: only FileNotFoundError means missing.
    A PermissionError on the board's stat keeps the record and refuses loudly."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    board = tmp_path / 'cache' / 'scratch' / 'run' / 'kanban.db'
    board.parent.mkdir(parents=True)
    _registered(board)
    record, = (tmp_path / 'worker-resources' / 'boards').glob('*.json')
    shutil.rmtree(board.parent)
    denied = {board.resolve(), board.resolve().parent}
    real_stat = Path.stat

    def denied_stat(self, *args, **kwargs):
        if self in denied:
            raise PermissionError(13, 'Permission denied', str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', denied_stat)
    other = tmp_path / 'cache' / 'scratch' / 'other' / 'kanban.db'
    other.parent.mkdir(parents=True)
    _registered(other)  # runs _retire_collected under the fence
    assert record.exists(), 'a permission error is not proof the board was collected'
    with pytest.raises(PermissionError):
        inventory.paths()


def _deny_os_stat(monkeypatch, *denied):
    """EACCES from os.stat for *denied*: Path.stat and Path.exists both go through it,
    so this reproduces an unsearchable directory on every Python version (and as root)."""
    blocked = {os.fspath(path) for path in denied}
    real_stat = os.stat

    def denied_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) in blocked:
            raise PermissionError(errno.EACCES, 'Permission denied', os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', denied_stat)


@pytest.mark.parametrize('denied', [
    ('worker-resources', 'boards'), ('kanban', 'boards'), ('kanban.db',)])
def test_unreadable_inventory_location_refuses_instead_of_dropping(monkeypatch, tmp_path, denied):
    """Only ENOENT is absence. Path.exists() returns False on EACCES on Python
    3.14+ (3.11-3.13 raise): paths() must raise, never return a census that
    silently dropped every board behind an unsearchable directory."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    (tmp_path / 'kanban.db').touch()
    named = tmp_path / 'kanban' / 'boards' / 'other' / 'kanban.db'
    named.parent.mkdir(parents=True)
    named.touch()
    foreign = tmp_path / 'foreign' / 'kanban.db'
    foreign.parent.mkdir()
    _registered(foreign)
    assert len(inventory.paths()) == 3
    _deny_os_stat(monkeypatch, tmp_path.joinpath(*denied))
    with pytest.raises(PermissionError):
        inventory.paths()


@pytest.mark.parametrize('parent', ['worker-resources', 'kanban'])
def test_regular_file_inventory_parent_refuses_instead_of_dropping(monkeypatch, tmp_path, parent):
    """ENOTDIR is not absence: a regular file where the inventory directory's
    parent belongs is a broken layout, so paths() raises NotADirectoryError
    (3.11-3.13 ``Path.exists()`` returned False and would drop the records)."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    (tmp_path / 'kanban.db').touch()
    (tmp_path / parent).write_text('not a directory')
    with pytest.raises(NotADirectoryError):
        inventory.paths()


def test_regular_file_board_slug_is_no_board(monkeypatch, tmp_path):
    """Contrast: ``_board_file`` absorbs ENOTDIR for a single slug entry only."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    boards = tmp_path / 'kanban' / 'boards'
    boards.mkdir(parents=True)
    (boards / 'stray').write_text('not a board directory')
    assert inventory.paths() == []


def test_unreadable_registration_record_is_not_rewritten(monkeypatch, tmp_path):
    """register() treats only a missing record as absent; EACCES propagates."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    board = tmp_path / 'board' / 'kanban.db'
    board.parent.mkdir()
    _registered(board)
    record = worker_board_retirement.record_paths(board.resolve())[0]
    _deny_os_stat(monkeypatch, record)
    with pytest.raises(PermissionError):
        _registered(board)


def test_retirement_missing_and_discard_treat_only_enoent_as_absent(monkeypatch, tmp_path):
    absent = tmp_path / 'absent.json'
    assert worker_board_retirement.missing(absent)
    worker_board_retirement.discard(absent)  # no-op, no race window between check and unlink
    present = tmp_path / 'present.json'
    present.write_text('"x"')
    assert not worker_board_retirement.missing(present)
    with monkeypatch.context() as denied:
        _deny_os_stat(denied, present)
        with pytest.raises(PermissionError):
            worker_board_retirement.missing(present)
    worker_board_retirement.discard(present)
    assert worker_board_retirement.missing(present)


def test_ephemeral_roots_never_cover_the_account(monkeypatch, tmp_path):
    account = tmp_path / 'home' / '.hermes'
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path / 'home'))
    roots = inventory._ephemeral_roots()
    assert (account / 'cache' / 'scratch').resolve() in roots
    assert not any(account.resolve().is_relative_to(root) for root in roots)


def test_tmpfs_root_is_ephemeral(monkeypatch, tmp_path):
    """/dev/shm is RAM-backed and boot-cleared: run-state-suite-local puts its shards' TMPDIR there."""
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path / 'home' / '.hermes')
    assert Path('/dev/shm').resolve() in inventory._ephemeral_roots()


def test_board_removed_from_a_tmpfs_root_retires_like_tmp(monkeypatch, tmp_path):
    """worker-1, 2026-11-13 16:09-16:26Z: a state-suite shard's board under /dev/shm/<run>/shard-5 stayed
    registered after the run deleted it, and every claim on the host deferred worker_resources_unknown."""
    account = tmp_path / 'account'
    account.mkdir()
    shm = tmp_path / 'shm'
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    monkeypatch.setattr(inventory, 'TMPFS_ROOTS', (shm,))
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path / 'not-shm'))
    shard = shm / 'state-suite-local-x' / 'shard-5' / 'tmpabc' / '.hermes'
    shard.mkdir(parents=True)
    _registered(shard / 'kanban.db')
    record, = (account / 'worker-resources' / 'boards').glob('*.json')
    shutil.rmtree(shm / 'state-suite-local-x')
    assert inventory.paths() == []  # the census no longer refuses every claim
    live = shm / 'live' / 'kanban.db'
    live.parent.mkdir(parents=True)
    _registered(live)  # the fenced path retires the collected record
    assert not record.exists()
    live.unlink()  # its directory survives: a moving board under the tmpfs root still refuses
    with pytest.raises(FileNotFoundError):
        inventory.paths()


def test_pytest_never_registers_in_the_real_account_inventory(monkeypatch, tmp_path):
    """monkeypatch.undo() drops conftest's policy_root isolation; register must still refuse."""
    monkeypatch.setattr(hermes_constants, '_account_home', lambda: tmp_path)
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path / '.hermes')
    board = tmp_path / '.hermes/profiles/production/kanban.db'
    board.parent.mkdir(parents=True)
    conn = sqlite3.connect(board)
    try:
        with pytest.raises(RuntimeError, match='real account worker inventory'):
            inventory.register(conn)
        # A child that stripped PYTEST_* still carries our isolation marker.
        monkeypatch.delenv('PYTEST_CURRENT_TEST')
        monkeypatch.delenv('PYTEST_VERSION', raising=False)
        monkeypatch.setenv('HERMES_TEST_ISOLATION', str(tmp_path))
        monkeypatch.setattr(hermes_state_guard, '_has_pytest_ancestor', lambda: False)
        with pytest.raises(RuntimeError, match='real account worker inventory'):
            inventory.register(conn)
        # ... or only its pytest ancestor.
        monkeypatch.delenv('HERMES_TEST_ISOLATION')
        monkeypatch.setattr(hermes_state_guard, '_has_pytest_ancestor', lambda: True)
        with pytest.raises(RuntimeError, match='real account worker inventory'):
            inventory.register(conn)
        # A production process registers normally.
        monkeypatch.setattr(hermes_state_guard, '_has_pytest_ancestor', lambda: False)
        inventory.register(conn)
    finally:
        conn.close()
    assert inventory.paths() == [board.resolve()]


def test_stripped_env_child_with_isolation_marker_is_refused(tmp_path):
    """A real child process whose env lost PYTEST_* but kept HERMES_TEST_ISOLATION."""
    repo = Path(inventory.__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith('PYTEST_')}
    env['HERMES_TEST_ISOLATION'] = str(tmp_path)
    env['PYTHONPATH'] = str(repo)
    child = (
        'import sqlite3, sys\n'
        'from pathlib import Path\n'
        'import hermes_constants, hermes_state_guard\n'
        'from hermes_cli import worker_account_boards as inventory, worker_compute_capacity as capacity\n'
        'home = Path(sys.argv[1])\n'
        'hermes_constants._account_home = lambda: home\n'
        "capacity.policy_root = lambda: home / '.hermes'\n"
        'hermes_state_guard._has_pytest_ancestor = lambda: False  # marker alone must refuse\n'
        "conn = sqlite3.connect(home / 'board.db')\n"
        'try:\n'
        '    inventory.register(conn)\n'
        'except RuntimeError as exc:\n'
        "    print('refused', exc)\n"
        '    sys.exit(0)\n'
        "print('registered')\n"
        'sys.exit(1)\n'
    )
    result = subprocess.run([sys.executable, '-c', child, str(tmp_path)], env=env, cwd=repo,
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'real account worker inventory' in result.stdout
    assert not (tmp_path / '.hermes' / 'worker-resources').exists()
