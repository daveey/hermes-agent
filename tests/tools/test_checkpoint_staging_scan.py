"""An excluded tree can exhaust the raw walk without exhausting the project guard.

The size cap must still exclude later large files BEFORE git add, not merely
remove their index entries after git has already read/compressed their contents.
"""
import hashlib
import os

from tools import checkpoint_manager as cm
from tools import checkpoint_staging as staging


def test_excluded_tree_does_not_hide_later_oversize_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, 'CHECKPOINT_BASE', tmp_path / 'checkpoints')
    monkeypatch.setattr(cm, '_MAX_FILES', 8)
    work = tmp_path / 'project'
    build = work / 'build'
    later = work / 'src'
    build.mkdir(parents=True)
    later.mkdir()
    for i in range(12):
        (build / f'ignored-{i}').write_text('excluded build product')
    (later / 'small.py').write_text('keep = True\n')
    data = b'x' * (1024 * 1024 + 1)
    (later / 'large.bin').write_bytes(data)

    # os.walk order is filesystem-dependent: put the real excluded directory
    # before src, without substituting either tree or scan's file/stat results.
    walk = os.walk

    def ordered_walk(*args, **kwargs):
        for root, dirs, files in walk(*args, **kwargs):
            dirs.sort()
            yield root, dirs, files

    monkeypatch.setattr(staging.os, 'walk', ordered_walk)
    assert cm._dir_file_count(str(work)) <= cm._MAX_FILES
    bounded = staging.scan_tree(str(work), len(data) - 1, cm._MAX_FILES)
    assert bounded.count > cm._MAX_FILES
    assert 'src/large.bin' not in bounded.oversize  # regression precondition
    complete = cm._staging_scan(str(work), len(data) - 1)
    assert 'src/large.bin' in complete.oversize
    assert 'src/small.py' in complete.files

    manager = cm.CheckpointManager(enabled=True, max_file_size_mb=1)
    assert manager.ensure_checkpoint(str(work))
    target = manager.list_checkpoints(str(work))[0]['hash']
    store = cm._store_path()
    ok, paths, error = cm._run_git(['ls-tree', '-r', '--name-only', target], store, str(work))
    assert ok, error
    assert paths.splitlines() == ['src/small.py']
    # A post-add index cleanup could produce the same tree while still storing
    # the huge blob. Its absence proves git never hashed it into this fresh store.
    oid = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
    exists, _, _ = cm._run_git(['cat-file', '-e', oid], store, str(work), allowed_returncodes={1})
    assert not exists

    (later / 'small.py').write_text('keep = False\n')
    manager.record_agent_write(str(later / 'small.py'))
    plan = manager._safe_restore_plan(str(work), target)
    assert plan['success'], plan
    assert 'src/small.py' in plan['restore']
    exists, _, _ = cm._run_git(['cat-file', '-e', oid], store, str(work), allowed_returncodes={1})
    assert not exists
