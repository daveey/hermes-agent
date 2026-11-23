"""Account source-custody authority store: existing-only, private, FULL/WAL, inert.

Real SQLite, real processes and real files under the isolated policy root.
Nothing here activates custody or produces operations from a production path.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_constants
from hermes_cli import kanban_source_pin_store as store
from hermes_cli import sqlite_safe_read
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli.kanban_source_barrier import CustodyBusy, CustodyUnavailable, hold
from tools.environments.local import build_subprocess_env

pytestmark = pytest.mark.platforms('posix')

REPO = Path(__file__).resolve().parents[2]
BOARD = 'b' * 32
RUN = json.dumps({'id': 7, 'started_at': 1700000000, 'claim': 'c-1', 'pid': 42,
                  'fingerprint': 'fp-1', 'runs_on': None})


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _op(n: int = 1, **over):
    fields = dict(op_id=f'{n:032x}', lookup_key=f'lk-{n}', board_uuid=BOARD, task_id='t_one',
                  source_host='host-a', original_run_json=RUN, kind='park',
                  scope_json='{"lineage":[]}')
    fields.update(over)
    return fields


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm'])
def test_transaction_revalidates_replaced_database_before_commit(tmp_path, suffix):
    store.setup()
    original = Path(str(store.store_path()) + suffix)
    database_before = store.store_path().read_bytes()
    retained = tmp_path / 'retained.sqlite3'
    with pytest.raises(store.StoreUnavailable):
        with store.transaction() as writer:
            writer.create_operation(**_op())
            assert original.exists()
            original.rename(retained)
            original.write_bytes(b'planted replacement')
            original.chmod(0o600)
    if not suffix:
        assert original.read_bytes() == b'planted replacement'
    else:
        # SQLite owns sidecar teardown on close; the transaction must not commit.
        assert store.store_path().read_bytes() == database_before
    assert retained.exists()


@pytest.mark.parametrize('kind', ['db-symlink', 'wal-symlink', 'wal-fifo', 'db-writable'])
def test_planted_sqlite_files_refuse_without_writes(tmp_path, kind):
    store.setup()
    database = store.store_path()
    before = database.read_bytes()
    if kind == 'db-symlink':
        target = tmp_path / 'target.sqlite3'
        database.rename(target)
        database.symlink_to(target)
    elif kind == 'db-writable':
        database.chmod(0o620)
    else:
        wal = Path(str(database) + '-wal')
        if wal.exists():
            wal.unlink()
        if kind == 'wal-fifo':
            os.mkfifo(wal)
        else:
            target = tmp_path / 'target-wal'
            target.write_bytes(b'unchanged')
            wal.symlink_to(target)
    with pytest.raises(store.StoreUnavailable):
        with store.transaction() as writer:
            writer.create_operation(**_op())
    assert database.read_bytes() == before
    if kind == 'wal-symlink':
        assert target.read_bytes() == b'unchanged'


@pytest.fixture
def ready():
    return store.setup()


def test_nested_marker_is_refused_without_recursive_json_escape():
    store.setup()
    store.marker_path().write_bytes(b'{"nested":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}')
    with pytest.raises(store.StoreUnavailable):
        store.status()


def _child(code: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = build_subprocess_env(extra={
        'PYTHONPATH': os.pathsep.join([str(REPO), os.environ.get('PYTHONPATH', '')]),
        'HERMES_TEST_POLICY_ROOT': str(capacity.policy_root()), **extra})
    return subprocess.run([sys.executable, '-c', code], cwd=str(REPO), env=env, text=True,
                          encoding='utf-8', capture_output=True, timeout=60, check=False)


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob('*')) if root.exists() else []


# ── setup ────────────────────────────────────────────────────────────────────

def test_setup_creates_private_store_and_consumer_only_marker():
    result = store.setup()
    path = store.store_path()
    assert path == capacity.policy_root() / 'source-custody' / 'pins.sqlite3'
    assert result.path == path and result.created is True
    assert _mode(path.parent) == 0o700 and _mode(path) == 0o600
    st = path.lstat()
    assert stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1
    marker = capacity.policy_root() / 'source-custody.activation.json'
    assert store.marker_path() == marker and not marker.parent.samefile(path.parent)
    assert _mode(marker) == 0o600
    data = json.loads(marker.read_text(encoding='utf-8'))
    assert data == {'version': 1, 'store_uuid': result.store_uuid, 'store_dev': st.st_dev,
                    'store_ino': st.st_ino, 'mode': 'consumer_only', 'capability_digest': None}
    assert len(result.store_uuid) == 32
    assert not store.store_path().is_relative_to(hermes_constants._account_home() / '.hermes')


def test_setup_is_idempotent_and_keeps_identity():
    first = store.setup()
    ino = store.store_path().lstat().st_ino
    marker_bytes = store.marker_path().read_bytes()
    second = store.setup()
    assert second.created is False and second.store_uuid == first.store_uuid
    assert store.store_path().lstat().st_ino == ino
    assert store.marker_path().read_bytes() == marker_bytes


def test_setup_refuses_the_real_account_home_before_any_io(monkeypatch):
    real = hermes_constants._account_home() / '.hermes'
    monkeypatch.setattr(capacity, 'policy_root', lambda: real)
    with pytest.raises(RuntimeError, match='pytest must not'):
        store.setup()
    with pytest.raises(RuntimeError, match='pytest must not'):
        store.status()
    with pytest.raises(RuntimeError, match='pytest must not'):
        with store.read():
            pass


def test_connection_is_full_wal_tracked_and_sidecars_are_private(ready):
    with store.transaction() as txn:
        assert txn.settings() == {'journal_mode': 'wal', 'synchronous': 2}
        txn.create_operation(**_op())
        assert sqlite_safe_read.has_live_connection(store.store_path())
        for suffix in ('-wal', '-shm'):
            side = store.store_path().with_name('pins.sqlite3' + suffix)
            assert _mode(side) == 0o600 and side.lstat().st_nlink == 1
    assert not sqlite_safe_read.has_live_connection(store.store_path())
    with store.read() as snap:
        assert snap.settings()['journal_mode'] == 'wal'
        assert sqlite_safe_read.has_live_connection(store.store_path())


def test_status_on_absent_store_initializes_nothing():
    before = _tree(capacity.policy_root())
    assert store.status() == {'state': 'absent'}
    assert _tree(capacity.policy_root()) == before
    with pytest.raises(CustodyUnavailable):
        with store.read():
            raise AssertionError('absent store must not open')
    with pytest.raises(CustodyUnavailable):
        with store.transaction():
            raise AssertionError('absent store must not open')
    assert _tree(capacity.policy_root()) == before


# ── interrupted / uncertain setup ────────────────────────────────────────────

def _seed_dir():
    from hermes_cli.kanban_source_barrier import establish
    establish()


@pytest.mark.parametrize('content', [b'', b'not sqlite at all' * 50])
def test_setup_refuses_uncertain_existing_file_and_retains_bytes(content):
    _seed_dir()
    path = store.store_path()
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(CustodyUnavailable):
        store.setup()
    assert path.read_bytes() == content
    assert not store.marker_path().exists()
    with pytest.raises(CustodyUnavailable):
        store.status()


def test_setup_interrupted_inside_schema_transaction_retains_file_and_refuses_retry(monkeypatch):
    from hermes_cli import kanban_source_store_schema as schema
    real_create = schema.create

    def crash(conn, store_uuid, created_at):
        conn.execute('CREATE TABLE meta(x)')
        raise OSError('disk went away')

    monkeypatch.setattr(schema, 'create', crash)
    with pytest.raises(Exception):
        store.setup()
    monkeypatch.setattr(schema, 'create', real_create)
    path = store.store_path()
    size, digest = path.stat().st_size, _sha(path)
    with pytest.raises(CustodyUnavailable):
        store.setup()
    assert (path.stat().st_size, _sha(path)) == (size, digest)
    assert not store.marker_path().exists()


def test_interrupted_before_marker_adopts_the_pristine_store_without_replacing_it(monkeypatch):
    def crash(*args, **kwargs):
        raise OSError('crash before marker')

    with monkeypatch.context() as local:    # never monkeypatch.undo(): it drops the policy isolation
        local.setattr(store, '_write_marker', crash)
        with pytest.raises(OSError):
            store.setup()
    ino = store.store_path().lstat().st_ino
    assert not store.marker_path().exists()
    result = store.setup()
    assert result.created is True
    assert store.store_path().lstat().st_ino == ino
    with store.read() as snap:
        assert snap.meta().store_uuid == result.store_uuid


def test_unmarked_store_with_content_is_uncertain_and_never_adopted():
    store.setup()
    with store.transaction() as txn:
        txn.create_operation(**_op())
    store.marker_path().unlink()
    digest = _sha(store.store_path())
    with pytest.raises(CustodyUnavailable):
        store.setup()
    assert _sha(store.store_path()) == digest and not store.marker_path().exists()


def test_setup_after_marker_never_recreates_a_missing_store(ready):
    store.store_path().unlink()
    with pytest.raises(CustodyUnavailable):
        store.setup()
    assert not store.store_path().exists()


def test_setup_never_runs_with_a_stray_sidecar_and_no_main_file():
    _seed_dir()
    side = store.store_path().with_name('pins.sqlite3-wal')
    side.write_bytes(b'left behind')
    side.chmod(0o600)
    with pytest.raises(CustodyUnavailable):
        store.setup()
    assert side.read_bytes() == b'left behind' and not store.store_path().exists()


# ── runtime identity and file validation ─────────────────────────────────────

def _expect_unavailable():
    with pytest.raises(CustodyUnavailable):
        with store.read():
            raise AssertionError('invalid authority must not open')
    with pytest.raises(CustodyUnavailable):
        with store.transaction():
            raise AssertionError('invalid authority must not open')
    with pytest.raises(CustodyUnavailable):
        store.status()


def test_missing_store_is_never_recreated(ready):
    store.store_path().unlink()
    _expect_unavailable()
    assert not store.store_path().exists()


def test_missing_marker_is_never_recreated(ready):
    store.marker_path().unlink()
    _expect_unavailable()
    assert not store.marker_path().exists()


def test_corrupt_store_is_unavailable_and_untouched(ready):
    path = store.store_path()
    path.write_bytes(b'\0garbage' * 1000)
    digest = _sha(path)
    _expect_unavailable()
    assert _sha(path) == digest


def test_replaced_store_inode_is_refused_even_with_identical_bytes(ready):
    path = store.store_path()
    copy = path.with_name('copy.sqlite3')
    shutil.copy2(path, copy)
    os.replace(copy, path)
    assert path.lstat().st_ino != json.loads(store.marker_path().read_text())['store_ino']
    _expect_unavailable()


def test_symlinked_store_is_refused(ready):
    path = store.store_path()
    real = path.with_name('elsewhere.sqlite3')
    os.rename(path, real)
    os.symlink(real, path)
    _expect_unavailable()


def test_symlinked_marker_is_refused(ready):
    marker = store.marker_path()
    real = marker.with_name('marker-real.json')
    os.rename(marker, real)
    os.symlink(real, marker)
    _expect_unavailable()


def test_hardlinked_store_is_refused(ready):
    os.link(store.store_path(), store.store_path().with_name('alias'))
    _expect_unavailable()


@pytest.mark.parametrize('target', ['store', 'marker', 'dir'])
def test_non_private_modes_are_refused(ready, target):
    path = {'store': store.store_path(), 'marker': store.marker_path(),
            'dir': store.store_path().parent}[target]
    path.chmod(0o755 if target == 'dir' else 0o644)
    _expect_unavailable()


def test_symlinked_wal_sidecar_is_refused(ready):
    with store.transaction() as txn:
        txn.create_operation(**_op())
    side = store.store_path().with_name('pins.sqlite3-wal')
    side.write_bytes(b'')
    side.chmod(0o600)
    other = side.with_name('real-wal')
    os.rename(side, other)
    os.symlink(other, side)
    _expect_unavailable()


def _rewrite_marker(**changes):
    path = store.marker_path()
    data = json.loads(path.read_text(encoding='utf-8'))
    for key, value in changes.items():
        if value is KeyError:
            data.pop(key)
        else:
            data[key] = value
    path.write_text(json.dumps(data), encoding='utf-8')
    path.chmod(0o600)


@pytest.mark.parametrize('changes', [
    {'store_uuid': 'a' * 32}, {'store_ino': 1}, {'store_dev': 1}, {'version': 2},
    {'mode': 'bogus'}, {'mode': 'ACTIVE'}, {'extra': 1}, {'capability_digest': KeyError},
    {'store_ino': True}, {'store_uuid': 'A' * 32},
])
def test_marker_must_match_store_exactly(ready, changes):
    _rewrite_marker(**changes)
    _expect_unavailable()


def test_malformed_marker_bytes_are_refused(ready):
    store.marker_path().write_bytes(b'{"version": 1')
    store.marker_path().chmod(0o600)
    _expect_unavailable()


@pytest.mark.parametrize('tamper', [
    'CREATE TABLE sneaky(x)',
    'UPDATE meta SET version=2',
    "UPDATE meta SET store_uuid='" + 'c' * 32 + "'",
    'DROP TABLE recovery',
])
def test_tampered_schema_or_meta_is_unavailable(ready, tamper):
    raw = sqlite3.connect(store.store_path())
    raw.execute(tamper)
    raw.commit()
    raw.close()
    _expect_unavailable()


def test_replaced_custody_directory_is_refused(ready):
    directory = store.store_path().parent
    moved = directory.with_name('source-custody-moved')
    os.rename(directory, moved)
    os.symlink(moved, directory)
    _expect_unavailable()


# ── busy / crash / reopen with real SQLite and processes ─────────────────────

def test_sqlite_busy_is_typed_retryable_not_corruption(ready):
    raw = sqlite3.connect(store.store_path(), isolation_level=None)
    raw.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(store.StoreBusy) as caught:
            with store.transaction():
                raise AssertionError('must not enter while another writer holds SQLite')
        assert not isinstance(caught.value, CustodyUnavailable)
        with store.read() as snap:           # WAL: readers are not blocked
            assert snap.operations_for_task(BOARD, 't_one') == []
        assert store.status()['state'] == 'ready'
    finally:
        raw.execute('ROLLBACK')
        raw.close()
    with store.transaction() as txn:         # nothing was damaged
        txn.create_operation(**_op())


def test_barrier_contention_is_busy_for_threads_and_processes(ready):
    with hold() as held:
        with pytest.raises(CustodyBusy):
            with store.transaction():
                raise AssertionError('independent open must not share the barrier')
        child = _child('from hermes_cli import kanban_source_pin_store as s\n'
                       'from hermes_cli.kanban_source_barrier import CustodyBusy\n'
                       'try:\n    s.transaction().__enter__()\nexcept CustodyBusy:\n    print("busy")\n')
        assert child.stdout.strip() == 'busy', child.stderr
        with store.transaction(held=held) as txn:   # caller's own held barrier is reused
            txn.create_operation(**_op())
    with store.read() as snap:
        assert snap.operation(_op()['op_id']) is not None


def test_process_killed_before_commit_leaves_nothing_and_store_reopens(ready):
    code = ('import os\nfrom hermes_cli import kanban_source_pin_store as s\n'
            f'm = s.transaction(); t = m.__enter__()\nt.create_operation(**{_op(5)!r})\n'
            'os._exit(9)\n')
    assert _child(code).returncode == 9
    with store.read() as snap:
        assert snap.operation(_op(5)['op_id']) is None
    with store.transaction() as txn:     # lock and SQLite writer slot were released by the kill
        txn.create_operation(**_op(6))


def test_process_killed_after_commit_is_durable_across_reopen(ready):
    code = ('import os\nfrom hermes_cli import kanban_source_pin_store as s\n'
            f'with s.transaction() as t:\n    t.create_operation(**{_op(5)!r})\n'
            'os._exit(9)\n')
    assert _child(code).returncode == 9
    with store.read() as snap:
        assert snap.operation(_op(5)['op_id']).phase == 'prepared'
    assert store.status(task_id='t_one')['operations'][0]['op_id'] == _op(5)['op_id']


def test_exception_rolls_back_every_write(ready):
    with pytest.raises(RuntimeError, match='boom'):
        with store.transaction() as txn:
            txn.create_operation(**_op())
            txn.put_unit(_op()['op_id'], 'u1', kind='candidate', state='pending', payload_json='{}')
            raise RuntimeError('boom')
    with store.read() as snap:
        assert snap.operation(_op()['op_id']) is None and snap.units(_op()['op_id']) == []


def test_reader_snapshot_does_not_see_a_later_commit(ready):
    with store.read() as snap:
        assert snap.operations_for_task(BOARD, 't_one') == []
        with store.transaction() as txn:
            txn.create_operation(**_op())
        assert snap.operations_for_task(BOARD, 't_one') == []
    with store.read() as snap:
        assert len(snap.operations_for_task(BOARD, 't_one')) == 1


# ── operations: lookup replay, CAS, immutability ─────────────────────────────

def test_duplicate_lookup_replay_returns_original_and_changes_nothing(ready):
    with store.transaction() as txn:
        first, created = txn.create_operation(**_op())
    assert created is True and first.revision == 1 and first.phase == 'prepared'
    with store.transaction() as txn:
        txn.update_operation(first.op_id, 1, phase='retained', scope_json='{"lineage":["x"]}')
    with store.transaction() as txn:
        replay, created = txn.create_operation(**_op(op_id='f' * 32))
        assert created is False and replay.op_id == first.op_id
        assert replay.phase == 'retained' and replay.revision == 2
        assert txn.operation('f' * 32) is None
    with store.read() as snap:
        assert len(snap.operations_for_task(BOARD, 't_one')) == 1


@pytest.mark.parametrize('change', [
    {'task_id': 't_two'}, {'board_uuid': 'a' * 32}, {'source_host': 'host-b'},
    {'kind': 'reclaim'}, {'original_run_json': '{"id": 8}'},
])
def test_lookup_collision_with_different_identity_is_invalid_not_replay(ready, change):
    with store.transaction() as txn:
        txn.create_operation(**_op())
    with pytest.raises(store.StoreInvalid):
        with store.transaction() as txn:
            txn.create_operation(**_op(op_id='e' * 32, **change))
    with store.read() as snap:
        assert snap.find_operation('lk-1').task_id == 't_one'


def test_same_op_id_with_other_lookup_key_is_invalid(ready):
    with store.transaction() as txn:
        txn.create_operation(**_op())
    with pytest.raises(store.StoreInvalid):
        with store.transaction() as txn:
            txn.create_operation(**_op(lookup_key='different'))


def test_stale_revision_is_rejected_and_state_is_unchanged(ready):
    with store.transaction() as txn:
        op, _ = txn.create_operation(**_op())
    with store.transaction() as txn:
        txn.update_operation(op.op_id, 1, phase='retained')
    with pytest.raises(store.StaleRevision):
        with store.transaction() as txn:
            txn.update_operation(op.op_id, 1, phase='settled')
    with store.read() as snap:
        now = snap.operation(op.op_id)
    assert (now.revision, now.phase) == (2, 'retained')


def test_cas_between_two_real_connections_has_one_winner(ready):
    with store.transaction() as txn:
        op, _ = txn.create_operation(**_op())
    with store.read() as stale_view:
        seen = stale_view.operation(op.op_id).revision
        with store.transaction() as txn:
            txn.update_operation(op.op_id, seen, scope_json='{"winner":1}')
    with pytest.raises(store.StaleRevision):
        with store.transaction() as txn:
            txn.update_operation(op.op_id, seen, scope_json='{"winner":2}')
    with store.read() as snap:
        assert snap.operation(op.op_id).scope_json == '{"winner":1}'


def test_phase_never_regresses_and_missing_operation_is_invalid(ready):
    with store.transaction() as txn:
        op, _ = txn.create_operation(**_op())
        txn.update_operation(op.op_id, 1, phase='settled')
    with pytest.raises(store.StoreInvalid):
        with store.transaction() as txn:
            txn.update_operation(op.op_id, 2, phase='prepared')
    with pytest.raises(store.StoreInvalid):
        with store.transaction() as txn:
            txn.update_operation('0' * 32, 1, phase='retained')


def test_unchanged_update_does_not_bump_revision_and_changes_bump_once(ready):
    with store.transaction() as txn:
        op, _ = txn.create_operation(**_op())
        same = txn.update_operation(op.op_id, 1, phase='prepared')
        assert same.revision == 1
        moved = txn.update_operation(op.op_id, 1, last_attempted=123, projection_revision=2)
        assert (moved.revision, moved.last_attempted, moved.projection_revision) == (2, 123, 2)
        with pytest.raises(store.StoreInvalid):
            txn.update_operation(op.op_id, 2, projection_revision=1)
        cleared = txn.update_operation(op.op_id, 2, last_attempted=None)
        assert cleared.last_attempted is None and cleared.revision == 3


@pytest.mark.parametrize('field,value', [
    ('op_id', 'short'), ('op_id', 'G' * 32), ('board_uuid', 'x'), ('task_id', ''),
    ('task_id', 't' * 129), ('source_host', ''), ('kind', 'delete'), ('phase', 'gone'),
    ('original_run_json', '[1]'), ('original_run_json', 'null'), ('original_run_json', '{bad'),
    ('original_run_json', '{"a":1,"a":2}'), ('original_run_json', '{"a":NaN}'),
    ('scope_json', '"s"'), ('lookup_key', ''), ('last_attempted', True),
    ('original_run_json', '{"command":"rm -rf /"}'), ('scope_json', '{"body":"card text"}'),
])
def test_invalid_input_is_rejected_without_echoing_it(ready, field, value):
    with pytest.raises(store.StoreInvalid) as caught:
        with store.transaction() as txn:
            txn.create_operation(**_op(**{field: value}))
    assert 'rm -rf' not in str(caught.value) and 'card text' not in str(caught.value)
    with store.read() as snap:
        assert snap.operations_for_task(BOARD, 't_one') == []


# ── raw runs_on losslessness and physical identity ───────────────────────────

RAW_VARIANTS = [
    '{"id":1,"runs_on":null}',
    '{"id":1,"runs_on":""}',
    '{"id":1}',
    '{"id": 1, "runs_on": "{\\"gpu\\": 2,  \\"arch\\":[\\"b\\", \\"a\\"]}"}',
    '{ "runs_on":"  pool \\u00e9\\n", "id" : 1 }',
    '{"id":1,"runs_on":"{\\"z\\":1,\\"a\\":{\\"n\\":null}}","extra":[1,2.50,1e3]}',
]


@pytest.mark.parametrize('raw', RAW_VARIANTS)
def test_original_run_json_round_trips_byte_for_byte(ready, raw):
    with store.transaction() as txn:
        txn.create_operation(**_op(original_run_json=raw))
    with store.read() as snap:
        assert snap.operation(_op()['op_id']).original_run_json == raw
    stored = sqlite3.connect(store.store_path()).execute(
        'SELECT original_run_json, typeof(original_run_json) FROM operations').fetchone()
    assert stored == (raw, 'text')


def test_null_empty_and_absent_runs_on_stay_distinguishable(ready):
    seen = {}
    for n, raw in enumerate(RAW_VARIANTS[:3], start=1):
        with store.transaction() as txn:
            txn.create_operation(**_op(n, original_run_json=raw))
    with store.read() as snap:
        for n in (1, 2, 3):
            seen[n] = json.loads(snap.operation(f'{n:032x}').original_run_json)
    assert seen[1]['runs_on'] is None and seen[2]['runs_on'] == '' and 'runs_on' not in seen[3]


def _key(**over):
    args = dict(board_uuid=BOARD, db_dev=1, db_ino=2, source_host='h', task_id='t', kind='park',
                run={'id': 1, 'started_at': 2, 'claim': 'c', 'pid': 3, 'fingerprint': 'f'})
    args.update(over)
    return store.lookup_key(**args)


def test_lookup_key_is_deterministic_and_tags_absent_values():
    assert _key() == _key() and len(_key()) == 64
    base = dict(id=1, started_at=2, claim='c', pid=3, fingerprint='f')
    absent = _key(run={**base, 'claim': None})
    assert len({absent, _key(run={**base, 'claim': ''}), _key(run={**base, 'claim': 'None'}),
                _key(run={k: v for k, v in base.items() if k != 'claim'}), _key()}) == 4
    assert _key(run={k: v for k, v in base.items() if k != 'claim'}) == absent
    for change in ({'db_ino': 3}, {'db_dev': 9}, {'board_uuid': 'a' * 32}, {'kind': 'reclaim'},
                   {'task_id': 'u'}, {'source_host': 'g'}):
        assert _key(**change) != _key()
    with pytest.raises(store.StoreInvalid):
        _key(run={**base, 'pid': True})
    with pytest.raises(store.StoreInvalid):
        _key(run={**base, 'bogus': 1})


def test_duplicate_run_uid_on_different_physical_inputs_are_separate_operations(ready):
    raw = '{"id":7,"run_uid":"same-uid","runs_on":null}'
    for n, ino in ((1, 100), (2, 200)):
        with store.transaction() as txn:
            txn.create_operation(**_op(n, original_run_json=raw,
                                       lookup_key=_key(db_ino=ino, run={'id': 7})))
    with store.transaction() as txn:   # replay of the first physical input
        _, created = txn.create_operation(**_op(9, original_run_json=raw,
                                                lookup_key=_key(db_ino=100, run={'id': 7})))
        assert created is False
    with store.read() as snap:
        union = snap.operations_for_task(BOARD, 't_one')
    assert [o.op_id for o in union] == [f'{1:032x}', f'{2:032x}']
    assert all(json.loads(o.original_run_json)['run_uid'] == 'same-uid' for o in union)


# ── units: immutable identity, tombstones, import replay ─────────────────────

def test_unit_create_replay_cas_and_conflict(ready):
    op_id = _op()['op_id']
    with store.transaction() as txn:
        txn.create_operation(**_op())
        unit, created = txn.put_unit(op_id, 'u1', kind='candidate', state='pending',
                                     payload_json='{"p":1}')
        assert created and unit.revision == 1
        again, created = txn.put_unit(op_id, 'u1', kind='candidate', state='pending',
                                      payload_json='{"p":1}')
        assert not created and again == unit
        with pytest.raises(store.StaleRevision):
            txn.put_unit(op_id, 'u1', kind='candidate', state='retained', payload_json='{"p":2}')
        moved, created = txn.put_unit(op_id, 'u1', kind='candidate', state='retained',
                                      payload_json='{"p":2}', expected_revision=1)
        assert not created and moved.revision == 2 and moved.payload_json == '{"p":2}'
        with pytest.raises(store.StaleRevision):
            txn.put_unit(op_id, 'u1', kind='candidate', state='published',
                         payload_json='{"p":3}', expected_revision=1)
        with pytest.raises(store.StoreInvalid):   # unit kind is part of immutable identity
            txn.put_unit(op_id, 'u1', kind='jobs', state='retained', payload_json='{}',
                         expected_revision=2)
        with pytest.raises(store.StoreInvalid):   # no unit for an operation that does not exist
            txn.put_unit('9' * 32, 'u1', kind='candidate', state='pending', payload_json=None)


@pytest.mark.parametrize('terminal', ['excluded', 'published'])
def test_terminal_unit_states_are_never_resurrected(ready, terminal):
    op_id = _op()['op_id']
    with store.transaction() as txn:
        txn.create_operation(**_op())
        txn.put_unit(op_id, 'u', kind='candidate', state='pending', payload_json='{}')
        txn.put_unit(op_id, 'u', kind='candidate', state=terminal, payload_json='{"why":"x"}',
                     expected_revision=1)
    for state in ('pending', 'retained') + (('excluded',) if terminal == 'published' else ('published',)):
        with pytest.raises(store.StoreInvalid):
            with store.transaction() as txn:
                txn.put_unit(op_id, 'u', kind='candidate', state=state, payload_json='{}',
                             expected_revision=2)
    with store.read() as snap:
        unit = snap.unit(op_id, 'u')
    assert (unit.state, unit.revision, unit.payload_json) == (terminal, 2, '{"why":"x"}')


def test_import_replay_never_overwrites_newer_units_or_tombstones(ready):
    op_id = _op()['op_id']
    with store.transaction() as txn:
        txn.create_operation(**_op())
        txn.put_unit(op_id, 'new', kind='candidate', state='pending', payload_json='{"v":1}')
        txn.put_unit(op_id, 'new', kind='candidate', state='published', payload_json='{"v":2}',
                     expected_revision=1)
        txn.put_unit(op_id, 'gone', kind='candidate', state='excluded', payload_json='{"d":"sib"}')
    with store.transaction() as txn:
        replay, inserted = txn.import_unit(op_id, 'new', kind='candidate', state='pending',
                                           payload_json='{"v":0}')
        assert not inserted and (replay.state, replay.revision) == ('published', 2)
        tomb, inserted = txn.import_unit(op_id, 'gone', kind='candidate', state='pending',
                                         payload_json='{"v":0}')
        assert not inserted and tomb.state == 'excluded' and tomb.payload_json == '{"d":"sib"}'
        fresh, inserted = txn.import_unit(op_id, 'fresh', kind='expand', state='pending',
                                          payload_json=None)
        assert inserted and fresh.payload_json is None


def test_imports_cursor_cas_and_completeness_never_regress(ready):
    with store.transaction() as txn:
        rec, created = txn.record_import('in-1', '{"pos":1}', complete=False)
        assert created and (rec.cursor_json, rec.complete) == ('{"pos":1}', False)
        same, created = txn.record_import('in-1', '{"pos":1}', complete=False)
        assert not created and same == rec
        with pytest.raises(store.StaleRevision):
            txn.record_import('in-1', '{"pos":2}', complete=False, expected_cursor_json='{"x":0}')
        txn.record_import('in-1', '{"pos":2}', complete=True, expected_cursor_json='{"pos":1}')
        done, created = txn.record_import('in-1', '{"pos":9}', complete=False,
                                          expected_cursor_json='{"pos":2}')
        assert done.complete is True and done.cursor_json == '{"pos":2}' and not created


def test_status_is_read_only_privacy_preserving_and_barrier_free(ready):
    secret = '{"id":1,"fingerprint":"SECRET-FP","runs_on":"{\\"gpu\\":1}"}'
    with store.transaction() as txn:
        op, _ = txn.create_operation(**_op(original_run_json=secret, scope_json='{"p":"PRIVATE"}'))
        txn.put_unit(op.op_id, 'u', kind='candidate', state='pending', payload_json='{"s":"PAYLOAD"}')
        txn.record_import('in-1', '{"pos":1}', complete=False)
    before = (_sha(store.store_path()), store.store_path().stat().st_mtime_ns)
    with hold():  # status must not need or disturb the account barrier
        full = store.status()
        scoped = store.status(task_id='t_one')
        other = store.status(task_id='t_missing')
    assert (_sha(store.store_path()), store.store_path().stat().st_mtime_ns) == before
    assert full['state'] == 'ready' and full['mode'] == 'consumer_only'
    assert full['operations'] == {'prepared': 1, 'retained': 0, 'settled': 0}
    assert full['incomplete_imports'] == 1
    assert [o['op_id'] for o in scoped['operations']] == [op.op_id]
    assert scoped['operations'][0]['units'] == {'pending': 1, 'retained': 0, 'published': 0, 'excluded': 0}
    assert other['operations'] == []
    blob = json.dumps([full, scoped, other])
    for private in ('SECRET-FP', 'PRIVATE', 'PAYLOAD', 'gpu'):
        assert private not in blob
    with store.read() as snap:
        assert snap.import_record('in-1').cursor_json == '{"pos":1}'
