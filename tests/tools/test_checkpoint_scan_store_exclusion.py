"""The staging scan and budget estimate see only what ``git add -A`` stages.

With HERMES_HOME as the working directory the checkpoint store sits inside the tree. The add
excludes it by pathspec, but a scan (and ``ls-files -o -m`` narrowing) that still counted the
store priced its own packfiles: as the store grew, HERMES_HOME checkpoints were skipped and
backed off for an hour. Default-excluded trees (``node_modules/``) are pruned the same way,
while tracked or re-included files in them stay size-capped.
"""
import os
import subprocess

import pytest

from tools import checkpoint_manager as cm
from tools import checkpoint_staging as staging

_STORE_JUNK = 30
_JUNK_BYTES = 900 * 1024  # 30 x 0.9 MB ~= 26 MB, the reproduced false estimate


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("checkpoints:\n  enabled: true\n")
    (home / "notes.md").write_text("one\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", home / "checkpoints")
    monkeypatch.setattr(
        cm, "selected_git_env", lambda base=None: dict(base if base is not None else os.environ),
    )
    # Simulated slow host: 26 MB costs ~52 s, over the 30 s budget; the real tree is ~instant.
    monkeypatch.setattr(staging, "ADD_BYTES_PER_SECOND", 512 * 1024)
    monkeypatch.setattr(staging, "load_factor", lambda: 1.0)
    monkeypatch.setattr(staging, "_slow_dirs", {})
    return home


def _grow_store(store):
    junk = store / "objects" / "pack"
    junk.mkdir(parents=True, exist_ok=True)
    for i in range(_STORE_JUNK):
        (junk / f"junk-{i}.bin").write_bytes(os.urandom(_JUNK_BYTES))


def test_growing_own_store_does_not_skip_hermes_home_checkpoints(home):
    mgr = cm.CheckpointManager(enabled=True)
    store = cm._store_path()
    assert cm._init_store(store, str(home)) is None
    _grow_store(store)

    scan = cm._staging_scan(str(home), 0)
    assert not [p for p in scan.files if p.startswith("checkpoints/")]
    assert staging.estimate(scan, None).seconds < staging.budget_s(cm._GIT_TIMEOUT * 2)

    assert mgr.ensure_checkpoint(str(home)), "first HERMES_HOME checkpoint skipped"
    assert staging.slow_remaining_s(store, str(home)) == 0

    # Warm turn: the store's files are fresher than the index; still not priced.
    _grow_store(store)
    (home / "notes.md").write_text("two\n")
    mgr.new_turn()
    assert mgr.ensure_checkpoint(str(home)), "warm HERMES_HOME checkpoint skipped"
    assert staging.slow_remaining_s(store, str(home)) == 0


def test_ls_files_narrowing_excludes_the_store(home):
    """Even a scan that counted the store must be narrowed by a store-excluding listing."""
    mgr = cm.CheckpointManager(enabled=True)
    store = cm._store_path()
    assert cm._init_store(store, str(home)) is None
    _grow_store(store)
    index_file = cm._index_path(store, cm._project_hash(str(home)))
    index_file.parent.mkdir(parents=True, exist_ok=True)
    unpruned = staging.scan_tree(str(home), 0, 10**9)  # no store skip: the old scan
    assert staging.estimate(unpruned, None).seconds > staging.budget_s(cm._GIT_TIMEOUT * 2)
    assert mgr._over_staging_budget(store, str(home), index_file, unpruned, None) is None


def test_default_excludes_are_pruned_not_walked(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    (work / "node_modules" / "pkg").mkdir(parents=True)
    (work / ".venv" / "lib").mkdir(parents=True)
    for i in range(50):
        (work / "node_modules" / "pkg" / f"f{i}.js").write_text("x")
        (work / ".venv" / "lib" / f"m{i}.py").write_text("x")
    (work / "app.log").write_text("x")
    (work / "main.py").write_text("x")
    walked = []
    real_walk = os.walk

    def spy(*a, **kw):
        for entry in real_walk(*a, **kw):
            walked.append(entry[0])
            yield entry

    monkeypatch.setattr(staging.os, "walk", spy)
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", tmp_path / "ckpt")
    scan = cm._staging_scan(str(work), 0)
    assert set(scan.files) == {"main.py"}
    assert not [d for d in walked if "node_modules" in d or ".venv" in d]
    assert scan.count == 1


def test_reincluded_and_tracked_oversize_files_in_pruned_dirs_stay_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", tmp_path / "ckpt")
    monkeypatch.setattr(
        cm, "selected_git_env", lambda base=None: dict(base if base is not None else os.environ),
    )
    cap = 1024
    work = tmp_path / "proj"
    (work / "build").mkdir(parents=True)
    (work / "dist").mkdir()
    (work / "build" / "big.bin").write_bytes(b"b" * (cap + 1))
    (work / "dist" / "big.bin").write_bytes(b"d" * (cap + 1))
    (work / "main.py").write_text("x")

    # A tracked path in a pruned dir: add -A re-reads it, so it must be capped.
    store = cm._store_path()
    assert cm._init_store(store, str(work)) is None
    index_file = cm._index_path(store, cm._project_hash(str(work)))
    index_file.parent.mkdir(parents=True, exist_ok=True)
    ok, _, err = cm._run_git(
        ["update-index", "--add", "--cacheinfo", "100644,e69de29bb2d1d6434b8b29ae775ad8c2e48c5391,build/big.bin"],
        store, str(work), index_file=index_file,
    )
    assert ok, err
    scan = cm._staging_scan(str(work), cap, store, index_file)
    assert "build/big.bin" in scan.oversize
    assert "dist/big.bin" not in scan.oversize  # untracked + excluded: git never reads it

    # A .gitignore re-include: git may stage the excluded name, so it is walked.
    (work / ".gitignore").write_text("!dist/\n")
    scan = cm._staging_scan(str(work), cap)
    assert "dist/big.bin" in scan.oversize
    out = subprocess.run(
        ["git", f"--git-dir={store}", f"--work-tree={work}", "ls-files", "-o", "--exclude-standard"],
        capture_output=True, text=True, check=True, env={**os.environ, "GIT_INDEX_FILE": str(tmp_path / "idx")},
    ).stdout.split()
    assert "dist/big.bin" in out  # the re-include really is honoured by git
