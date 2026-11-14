"""Legacy flat worker scope ownership: bound, fail-closed, never aliasing."""
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import worker_compute_capacity as capacity
from hermes_cli import worker_legacy_scopes as legacy
from hermes_cli import worker_resource_admission as admission


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    pool = tmp_path / 'pool'
    pool.mkdir()
    boot = tmp_path / 'boot_id'
    boot.write_text('boot-a\n')
    db = tmp_path / 'kanban.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)')
        conn.executemany('INSERT INTO tasks VALUES (?, ?)', [('t1', 'parked'), ('t2', 'done')])
    monkeypatch.setattr(capacity, 'policy_root', lambda: home)
    monkeypatch.setattr(capacity, 'pool_path', lambda: pool)
    monkeypatch.setattr(legacy, 'BOOT_ID_PATH', boot)
    monkeypatch.setattr('hermes_constants.get_default_hermes_root', lambda: home)
    return home, pool, boot, db


def scope(pool, name='old-job.scope', populated=1, cpu='200000 100000', weight='10'):
    path = pool / name
    path.mkdir(exist_ok=True)
    (path / 'cgroup.events').write_text(f'populated {populated}\n')
    (path / 'cpu.max').write_text(cpu + '\n')
    (path / 'cpu.weight').write_text(weight + '\n')
    return path


def record_file(home):
    [path] = (home / 'worker-resources' / 'legacy-owners').iterdir()
    return path


def test_register_binds_owner_and_lookup(env):
    _, pool, _, db = env
    scope(pool)
    record = legacy.register('old-job.scope', db, 't1', pool=pool)
    assert record['owner'] == [str(db.resolve()), 't1']
    assert legacy.lookup(pool, 'old-job.scope') == (str(db.resolve()), 't1')
    assert legacy.owners_for_pool(pool) == {'old-job.scope': (str(db.resolve()), 't1')}


@pytest.mark.parametrize('kwargs', [
    {'populated': 0}, {'cpu': 'max 100000'}, {'cpu': '300000 100000'}, {'weight': '11'}, {'weight': '0'},
])
def test_register_refuses_unsafe_scope(env, kwargs):
    _, pool, _, db = env
    scope(pool, **kwargs)
    with pytest.raises(ValueError):
        legacy.register('old-job.scope', db, 't1', pool=pool)


@pytest.mark.parametrize('name', ['../x', 'a/b', '.', '..', '.hidden', '', 'x' * 300,
                                  'hermes-kanban-' + 'a' * 64 + '.slice'])
def test_register_refuses_bad_names(env, name):
    _, pool, _, db = env
    with pytest.raises(ValueError):
        legacy.register(name, db, 't1', pool=pool)


def test_register_refuses_symlink_escape_and_bad_owner(env, tmp_path):
    _, pool, _, db = env
    outside = scope(tmp_path, 'outside')
    (pool / 'link.scope').symlink_to(outside)
    with pytest.raises(ValueError):
        legacy.register('link.scope', db, 't1', pool=pool)
    scope(pool)
    with pytest.raises(ValueError):
        legacy.register('old-job.scope', 'relative.db', 't1', pool=pool)
    with pytest.raises(ValueError):
        legacy.register('old-job.scope', tmp_path / 'missing.db', 't1', pool=pool)


def test_register_is_fenced_and_does_not_touch_scope(env, monkeypatch):
    home, pool, _, db = env
    path = scope(pool)
    before = {p.name: p.read_text() for p in path.iterdir()}
    from hermes_cli.admission_fence import admission_lock
    import hermes_cli.admission_fence as fence
    with admission_lock(home):
        monkeypatch.setattr(fence, 'admission_lock', lambda root, timeout=0: admission_lock(root, timeout=0.01))
        with pytest.raises(fence.AdmissionBusy):
            legacy.register('old-job.scope', db, 't1', pool=pool)
    legacy.register('old-job.scope', db, 't1', pool=pool)
    assert {p.name: p.read_text() for p in path.iterdir()} == before


def test_reregister_other_owner_refused_while_live(env):
    _, pool, _, db = env
    scope(pool)
    legacy.register('old-job.scope', db, 't1', pool=pool)
    legacy.register('old-job.scope', db, 't1', pool=pool)
    with pytest.raises(ValueError):
        legacy.register('old-job.scope', db, 't2', pool=pool)


def test_stale_records_are_ignored(env):
    home, pool, boot, db = env
    scope(pool)
    legacy.register('old-job.scope', db, 't1', pool=pool)
    # recreated scope (different inode)
    path = record_file(home)
    data = json.loads(path.read_text())
    data['ino'] += 1
    path.write_text(json.dumps(data))
    assert legacy.lookup(pool, 'old-job.scope') is None
    data['ino'] -= 1
    path.write_text(json.dumps(data))
    assert legacy.lookup(pool, 'old-job.scope') is not None
    # reboot
    boot.write_text('boot-b\n')
    assert legacy.lookup(pool, 'old-job.scope') is None
    # disappeared, and re-registration replaces the stale record
    boot.write_text('boot-a\n')
    (pool / 'old-job.scope' / 'cgroup.events').unlink()
    (pool / 'old-job.scope' / 'cpu.max').unlink()
    (pool / 'old-job.scope' / 'cpu.weight').unlink()
    (pool / 'old-job.scope').rmdir()
    assert legacy.lookup(pool, 'old-job.scope') is None
    # The recreated directory's inode is up to the filesystem and can be the old
    # one + 1 (a freed child inode reused under parallel load), so derive the
    # stale identity from the scope that actually exists now.
    recreated = scope(pool)
    data['ino'] = recreated.lstat().st_ino + 1
    path.write_text(json.dumps(data))
    assert legacy.register('old-job.scope', db, 't2', pool=pool)['owner'][1] == 't2'


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(scope='other'), lambda d: d.update(owner=['rel', 't']), lambda d: d.update(ino='1'),
    lambda d: d.update(version=True), lambda d: d.update(version=1.0),
    lambda d: d.pop('boot_id'), lambda d: d.update(extra=1), lambda d: d.update(version=2),
])
def test_corrupt_record_fails_closed(env, mutate):
    home, pool, _, db = env
    scope(pool)
    legacy.register('old-job.scope', db, 't1', pool=pool)
    path = record_file(home)
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        legacy.lookup(pool, 'old-job.scope')
    assert admission.refusal() in {None, 'worker_resources_unknown'}  # never raises
    path.write_text('{broken')
    with pytest.raises(ValueError):
        legacy.owners_for_pool(pool)


def test_census_wiring_and_unknown_stays_reserved(env, monkeypatch):
    from hermes_cli import machine_metrics
    home, pool, boot, db = env
    monkeypatch.setattr('hermes_cli.worker_account_boards.paths', list)
    def fake_active(exclude=None, owner_output=None, board_paths=None):
        assert board_paths == []
        owner_output.add((str(db.resolve()), 't1'))
        return 1
    monkeypatch.setattr(machine_metrics, 'active_card_runs', fake_active)
    scope(pool)
    # unknown: active t1 plus an unmapped populated scope => 2
    assert admission.occupied_count_for_host() == 2
    # registered to the active task: counted once
    legacy.register('old-job.scope', db, 't1', pool=pool)
    assert admission.occupied_count_for_host() == 1
    # reboot makes the map stale: unknown again, reserved
    boot.write_text('boot-b\n')
    assert admission.occupied_count_for_host() == 2
    # corrupt record on a live scope fails the census (refusal fails closed)
    boot.write_text('boot-a\n')
    record_file(home).write_text('{broken')
    with pytest.raises(ValueError):
        admission.occupied_count_for_host()
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
    monkeypatch.setattr('os.getloadavg', lambda: (1, 1, 1))
    assert admission.refusal() == 'worker_resources_unknown'


@pytest.mark.parametrize('boundary', ['before_lookup', 'lookup', 'events'])
def test_census_refuses_replaced_scope(env, monkeypatch, boundary):
    _, pool, _, db = env
    path = scope(pool)
    legacy.register(path.name, db, 't1', pool=pool)
    original_read = Path.read_text
    original_lookup = legacy.lookup
    def replace_scope():
        path.rename(pool / 'removed')
        scope(pool)
    def lookup(root, name):
        if boundary == 'before_lookup':
            replace_scope()
        owner = original_lookup(root, name)
        if boundary == 'lookup':
            replace_scope()
        return owner
    def read(target, *args, **kwargs):
        result = original_read(target, *args, **kwargs)
        if boundary == 'events' and target == path / 'cgroup.events':
            replace_scope()
        return result
    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(ValueError, match='changed'):
        admission.pool_occupancy(pool, {}, set(), legacy_resolver=lookup)


def test_census_refuses_disappeared_scope(env, monkeypatch):
    _, pool, _, _ = env
    path = scope(pool)
    original = Path.read_text
    def read(target, *args, **kwargs):
        result = original(target, *args, **kwargs)
        if target == path / 'cgroup.events':
            path.rename(pool.parent / 'removed')
        return result
    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(ValueError, match='changed'):
        capacity.occupied_owners(pool, {}, set())


def test_registration_requires_exact_existing_task(env):
    _, pool, _, db = env
    scope(pool)
    with pytest.raises(ValueError, match='task'):
        legacy.register('old-job.scope', db, 't', pool=pool)


@pytest.mark.parametrize('kind', ['empty', 'corrupt', 'no_tasks', 'bad_tasks'])
def test_registration_requires_valid_database(env, kind):
    _, pool, _, db = env
    scope(pool)
    db.unlink()
    if kind == 'corrupt':
        db.write_text('not sqlite')
    elif kind == 'empty':
        db.touch()
    else:
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE other (id TEXT)' if kind == 'no_tasks'
                         else 'CREATE TABLE tasks (wrong TEXT)')
    before = db.read_bytes()
    with pytest.raises(ValueError, match='database'):
        legacy.register('old-job.scope', db, 't1', pool=pool)
    assert db.read_bytes() == before


def test_register_refuses_scope_replaced_during_validation(env, monkeypatch):
    home, pool, _, db = env
    path = scope(pool)
    original = capacity._verify_quota
    def verify(target, maximum):
        original(target, maximum)
        path.rename(pool / 'removed')
        scope(pool)
    monkeypatch.setattr(capacity, '_verify_quota', verify)
    with pytest.raises(ValueError, match='changed'):
        legacy.register(path.name, db, 't1', pool=pool)
    assert not (home / 'worker-resources' / 'legacy-owners').exists()


def test_registration_directory_fsync_failure_is_not_success(env, monkeypatch):
    import os
    import stat
    _, pool, _, db = env
    scope(pool)
    original = os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory fsync failed')
        original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises(OSError, match='directory fsync failed'):
        legacy.register('old-job.scope', db, 't1', pool=pool)


def test_multiple_scopes_for_one_owner_count_once(env):
    _, pool, _, db = env
    for name in ['old-job.scope', 'second.scope']:
        scope(pool, name)
        legacy.register(name, db, 't1', pool=pool)
    owner = (str(db.resolve()), 't1')
    assert capacity.occupied_owners(pool, {}, {owner}, legacy_resolver=legacy.lookup) == {owner}


def test_cli_register(env, capsys):
    _, pool, _, db = env
    scope(pool)
    assert legacy.main(['register', '--scope', 'old-job.scope', '--database', str(db), '--task', 't1']) == 0
    assert json.loads(capsys.readouterr().out)['scope'] == 'old-job.scope'
    assert legacy.main(['register', '--scope', '../x', '--database', str(db), '--task', 't1']) == 1
