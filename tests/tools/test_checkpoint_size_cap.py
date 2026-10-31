"""Real history retention and failure propagation at the public pruning callers."""
from __future__ import annotations

import contextlib
import os

import pytest

import tools.checkpoint_manager as checkpoints
from tools import checkpoint_maintenance


@pytest.fixture
def history(tmp_path, monkeypatch):
    base = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoints, "CHECKPOINT_BASE", base)
    project = min((tmp_path / name for name in ("project-a", "project-b")),
                  key=lambda path: checkpoints._project_hash(str(path)))
    project.mkdir()
    manager = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
    blob = project / "old.bin"
    blob.write_bytes(os.urandom(1_300_000))
    assert manager.ensure_checkpoint(str(project), "large-oldest")
    blob.unlink()
    for index in range(5):
        (project / "small.txt").write_text(str(index), encoding="utf-8")
        manager.new_turn()
        assert manager.ensure_checkpoint(str(project), f"small-{index}")
    return base, project, manager


def test_maintenance_size_cap_retains_every_snapshot_after_the_only_large_one(history):
    base, project, manager = history
    store = checkpoints._store_path(base)
    assert checkpoints._dir_size_bytes(store) > 1024 * 1024
    result = checkpoint_maintenance.prune_checkpoints(retention_days=0, checkpoint_base=base, max_total_size_mb=1)
    assert result["errors"] == 0
    assert [row["reason"] for row in manager.list_checkpoints(str(project))] == [f"small-{i}" for i in reversed(range(5))]
    assert checkpoints._dir_size_bytes(store) <= 1024 * 1024
    assert (project / "small.txt").read_text(encoding="utf-8") == "4"


def test_snapshot_over_cap_drops_one_round_and_leaves_the_gc_to_the_prune(history):
    """A checkpoint never waits on ``git gc``: over the cap it rewrites the ref once and marks
    the store gc-pending; the periodic prune reclaims the objects and clears the marker."""
    from tools.checkpoint_pruning import GC_PENDING_NAME

    base, project, manager = history
    store = checkpoints._store_path(base)
    manager.max_total_size_mb = 1
    gc_calls = []
    original = checkpoints._run_git

    def count_gc(args, *rest, **kwargs):
        if args[0] == "gc":
            gc_calls.append(args)
        return original(args, *rest, **kwargs)

    (project / "new.txt").write_text("new snapshot", encoding="utf-8")
    manager.new_turn()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(checkpoints, "_run_git", count_gc)
        assert manager.ensure_checkpoint(str(project), "newest")
    assert gc_calls == []
    assert (store / GC_PENDING_NAME).exists()
    assert [row["reason"] for row in manager.list_checkpoints(str(project))] == \
        ["newest"] + [f"small-{i}" for i in reversed(range(5))]
    # Objects are still in the pack until the prune runs.
    assert checkpoints._dir_size_bytes(store) > 1024 * 1024

    result = checkpoint_maintenance.prune_checkpoints(retention_days=0, checkpoint_base=base, max_total_size_mb=1)
    assert result["errors"] == 0
    assert not (store / GC_PENDING_NAME).exists()
    assert checkpoints._dir_size_bytes(store) <= 1024 * 1024
    assert (project / "small.txt").read_text(encoding="utf-8") == "4"


@contextlib.contextmanager
def _recording_git():
    calls = []
    original = checkpoints._run_git

    def record(args, *rest, **kwargs):
        calls.append(list(args))
        return original(args, *rest, **kwargs)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(checkpoints, "_run_git", record)
        yield calls


def test_snapshot_over_cap_never_reads_or_rewrites_another_projects_history(history):
    """A take over the cap costs O(1) Git calls: it never walks every project's ref. A store with
    hundreds of projects ran one ``rev-list`` per ref inside the tool call (30 s timeouts on a loaded
    laptop) and, turn after turn, flattened every project to a single snapshot."""
    _, first, manager = history
    other = next(first.parent / name for name in ("project-a", "project-b") if first.name != name)
    other.mkdir()
    for index in range(3):
        (other / "small.txt").write_text(str(index), encoding="utf-8")
        manager.new_turn()
        assert manager.ensure_checkpoint(str(other), f"other-{index}")
    before = manager.list_checkpoints(str(other))
    other_ref = checkpoints._ref_name(checkpoints._project_hash(str(other)))

    manager.max_total_size_mb = 1
    (first / "new.txt").write_text("new snapshot", encoding="utf-8")
    manager.new_turn()
    with _recording_git() as calls:
        assert manager.ensure_checkpoint(str(first), "newest")

    assert not [c for c in calls if c[0] == "for-each-ref"], calls
    assert not [c for c in calls if other_ref in c], calls
    assert manager.list_checkpoints(str(other)) == before


def test_snapshot_does_not_drop_again_while_the_last_drop_awaits_its_gc(history):
    """Until a gc reclaims the last drop the store size cannot move, so a second over-cap take
    must not drop another snapshot: re-measuring an unchanged store every turn erased history."""
    _, project, manager = history
    manager.max_total_size_mb = 1
    for index in range(3):
        (project / "new.txt").write_text(f"new {index}", encoding="utf-8")
        manager.new_turn()
        assert manager.ensure_checkpoint(str(project), f"newest-{index}")
    reasons = [row["reason"] for row in manager.list_checkpoints(str(project))]
    # One drop (the large oldest) for the whole gc cycle; every later snapshot is kept.
    assert reasons == [f"newest-{i}" for i in reversed(range(3))] + [f"small-{i}" for i in reversed(range(5))]

    (project / "new.txt").write_text("again", encoding="utf-8")
    manager.new_turn()
    with _recording_git() as calls:
        assert manager.ensure_checkpoint(str(project), "again")
    assert not [c for c in calls if c[0] == "for-each-ref"], calls
    # Only the bounded count probe of this project's own ref; no full history walk.
    assert all(any(a.startswith("--max-count=") for a in c) for c in calls if c[0] == "rev-list"), calls
    assert len([c for c in calls if c[0] == "update-ref"]) == 1  # the new snapshot only
    assert len(manager.list_checkpoints(str(project))) == 9


def test_gc_pending_for_days_is_reported_once(history, caplog):
    """A store whose deferred gc never runs (checkpoints.auto_prune off) says so, once per process."""
    import logging
    import time as _time
    from tools.checkpoint_pruning import GC_PENDING_NAME, GC_STALE_REPORTED

    base, project, manager = history
    store = checkpoints._store_path(base)
    marker = store / GC_PENDING_NAME
    marker.touch()
    old = _time.time() - 3 * 86400
    os.utime(marker, (old, old))
    GC_STALE_REPORTED.clear()
    caplog.set_level(logging.WARNING, logger="tools.checkpoint_manager")
    for index in range(2):
        (project / "new.txt").write_text(f"n{index}", encoding="utf-8")
        manager.new_turn()
        assert manager.ensure_checkpoint(str(project), f"n{index}")
    hits = [r for r in caplog.records if "hermes checkpoints prune" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    assert str(store) in hits[0].getMessage()


def test_reclaim_stops_before_touching_another_projects_history(history):
    base, first, manager = history
    # Real ref sorting chooses the first victim. Pick the other project so
    # this is a discriminating reclaim-before-next-project assertion.
    other = next(first.parent / name for name in ("project-a", "project-b") if first.name != name)
    other.mkdir()
    for index in range(3):
        (other / "small.txt").write_text(str(index), encoding="utf-8")
        manager.new_turn()
        assert manager.ensure_checkpoint(str(other), f"other-{index}")
    before = manager.list_checkpoints(str(other))
    result = checkpoint_maintenance.prune_checkpoints(retention_days=0, checkpoint_base=base, max_total_size_mb=1)
    assert result["errors"] == 0
    assert manager.list_checkpoints(str(other)) == before
    assert len(manager.list_checkpoints(str(first))) == 5


@pytest.mark.parametrize("failure", ["gc", "reflog", "for-each-ref", "rev-list", "log", "update-ref", "delete-ref"])
def test_maintenance_stops_on_failed_git_without_losing_a_second_snapshot(history, monkeypatch, failure):
    import json

    base, project, manager = history
    original = checkpoint_maintenance._run_git
    rewrites = []
    retention = 0
    if failure == "delete-ref":
        meta = checkpoints._project_meta_path(checkpoints._store_path(base), checkpoints._project_hash(str(project)))
        state = json.loads(meta.read_text(encoding="utf-8"))
        state["last_touch"] = 1
        meta.write_text(json.dumps(state), encoding="utf-8")
        retention = 1

    def fail_one(args, *rest, **kwargs):
        if args[0] == failure or (failure == "delete-ref" and args[:2] == ["update-ref", "-d"]):
            return False, "", "injected Git failure"
        result = original(args, *rest, **kwargs)
        if args[0] == "update-ref" and result[0]:
            rewrites.append(args)
        return result

    with monkeypatch.context() as patcher:
        patcher.setattr(checkpoint_maintenance, "_run_git", fail_one)
        result = checkpoint_maintenance.prune_checkpoints(retention_days=retention, checkpoint_base=base, max_total_size_mb=1)
    assert result["errors"] > 0
    assert len(rewrites) <= 1
    assert len(manager.list_checkpoints(str(project))) >= 5
    if failure == "delete-ref":
        assert meta.is_file()
        assert result["deleted_stale"] == 0


def test_snapshot_pruning_stops_at_a_failed_count_rewrite(history, monkeypatch, caplog):
    """A count trim that cannot rewrite its ref aborts the take-path pruning before the size
    round runs — never a partial history, never a second rewrite on top of a failed one."""
    _, project, manager = history
    manager.max_snapshots = 5
    manager.max_total_size_mb = 1
    (project / "later.txt").write_text("newest", encoding="utf-8")
    manager.new_turn()
    original = checkpoints._run_git
    changed_refs = []

    def refuse_rewrite(args, *rest, **kwargs):
        # The rewrite re-creates commits under their original dates; the snapshot's own
        # commit-tree carries no extra_env.
        if args[0] == "commit-tree" and kwargs.get("extra_env"):
            return False, "", "injected rewrite failure"
        result = original(args, *rest, **kwargs)
        if args[0] == "update-ref" and result[0]:
            changed_refs.append(args)
        return result

    monkeypatch.setattr(checkpoints, "_run_git", refuse_rewrite)
    assert manager.ensure_checkpoint(str(project), "latest")
    assert len(changed_refs) == 1  # The new checkpoint only; the trim failed before update-ref.
    assert "injected rewrite failure" in caplog.text
    store = checkpoints._store_path(checkpoints.CHECKPOINT_BASE)
    ref = checkpoints._ref_name(checkpoints._project_hash(str(project)))
    ok, out, _ = original(["rev-list", "--count", ref], store, str(project))
    assert ok and int(out) == 7  # Nothing trimmed: the failed rewrite left history whole.


def test_restore_keeps_its_target_alive_while_taking_the_safety_snapshot(history):
    _, project, manager = history
    target = manager.list_checkpoints(str(project))[-1]["hash"]
    manager.max_snapshots = 1
    (project / "small.txt").write_text("changed before restore", encoding="utf-8")
    result = manager.restore(str(project), target)
    assert result["success"], result
    assert (project / "old.bin").stat().st_size == 1_300_000
