"""An over-cap store must not make every checkpoint pay one Git call per project.

Regression for agent-23a0c3b69f: a 15 GB store against a 500 MB cap made each checkpoint run
``rev-list`` for all ~780 projects (``Pruner.drop_one_round``) and time out at 30 s under load,
while the only step that frees space (``git gc``) lived in a periodic prune gated on
``checkpoints.auto_prune``, which was off, and whose gc was killed at 90 s on a 14 GB store. The
take path touches only its own project (``Pruner.drop_oldest_once``) and defers the gc
(``.gc-pending``); the periodic prune reclaims even with ``auto_prune: false``, its gc runs niced
with a maintenance-length timeout, and a daily ERROR names a store stuck over its cap.
"""
from __future__ import annotations

import logging
import os
import time

import hermes_yaml as yaml
import pytest

import tools.checkpoint_manager as checkpoints
from hermes_constants import get_hermes_home
from tools import checkpoint_maintenance

from tools.checkpoint_pruning import GC_PENDING_NAME, Pruner


def _write_config(**section) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"checkpoints": section}), encoding="utf-8")


def _over_cap_store(tmp_path, monkeypatch, projects: int):
    """``projects`` projects with two snapshots each, plus one 1.3 MB incompressible blob."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    base = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoints, "CHECKPOINT_BASE", base)
    manager = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
    for index in range(projects):
        work = tmp_path / f"project-{index:03d}"
        work.mkdir()
        if index == 0:
            (work / "blob.bin").write_bytes(os.urandom(1_300_000))
        for turn in range(2):
            (work / "note.txt").write_text(f"{index}-{turn}", encoding="utf-8")
            manager.new_turn()
            assert manager.ensure_checkpoint(str(work), f"seed-{turn}")
    assert checkpoints._dir_size_bytes(checkpoints._store_path(base)) > 1024 * 1024
    return base, manager


def _git_calls_for_one_checkpoint(tmp_path, monkeypatch, projects: int) -> int:
    _base, manager = _over_cap_store(tmp_path, monkeypatch, projects)
    manager.max_total_size_mb = 1
    target = tmp_path / "project-000"
    (target / "note.txt").write_text("the checkpoint under test", encoding="utf-8")
    manager.new_turn()
    calls = []
    original = checkpoints._run_git

    def counting(args, *rest, **kwargs):
        calls.append(args[0])
        return original(args, *rest, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(checkpoints, "_run_git", counting)
        assert manager.ensure_checkpoint(str(target), "measured")
    return len(calls)


def test_over_cap_checkpoint_cost_does_not_grow_with_project_count(tmp_path, monkeypatch):
    few = _git_calls_for_one_checkpoint(tmp_path / "few", monkeypatch, 5)
    many = _git_calls_for_one_checkpoint(tmp_path / "many", monkeypatch, 50)
    assert many == few, f"5 projects: {few} git calls, 50 projects: {many} git calls"


def test_periodic_prune_enforces_the_cap_with_auto_prune_off(tmp_path, monkeypatch):
    """The MacBook path: ``auto_prune: false``, store over its cap, never pruned."""
    base, manager = _over_cap_store(tmp_path, monkeypatch, 3)
    manager.max_total_size_mb = 1
    target = tmp_path / "project-000"
    (target / "blob.bin").unlink()
    (target / "note.txt").write_text("blob deleted", encoding="utf-8")
    manager.new_turn()
    assert manager.ensure_checkpoint(str(target), "newest")
    store = checkpoints._store_path(base)
    assert (store / GC_PENDING_NAME).exists()
    _write_config(enabled=True, auto_prune=False, max_total_size_mb=1)

    gc_calls = []
    original = checkpoints._run_git

    def spy(args, *rest, **kwargs):
        if args[0] == "gc":
            gc_calls.append(kwargs)
        return original(args, *rest, **kwargs)

    monkeypatch.setattr(checkpoint_maintenance, "_run_git", spy)
    out = checkpoint_maintenance.auto_prune_from_config()
    assert out.get("skipped") is False, out
    assert out["result"]["errors"] == 0, out
    assert (base / ".last_prune").exists()
    assert checkpoints._dir_size_bytes(store) <= 1024 * 1024
    assert not (store / GC_PENDING_NAME).exists()
    # The maintenance gc is niced and gets the long maintenance budget, not 3x the tool timeout.
    assert gc_calls and all(c.get("timeout", 0) >= 1800 and c.get("low_priority") for c in gc_calls)
    # auto_prune is off: retention never deleted a project (the size cap may still trim history).
    assert manager.list_checkpoints(str(tmp_path / "project-002"))


def test_auto_prune_off_without_pending_size_work_stays_skipped(tmp_path, monkeypatch):
    base, _ = _over_cap_store(tmp_path, monkeypatch, 1)
    _write_config(enabled=True, auto_prune=False, max_total_size_mb=500)
    assert checkpoint_maintenance.auto_prune_from_config().get("skipped") is True
    assert not (base / ".last_prune").exists()


@pytest.mark.parametrize("last_prune_age_h", [None, 72])
def test_store_stuck_over_cap_logs_one_error_per_day(tmp_path, monkeypatch, caplog, last_prune_age_h):
    base, _ = _over_cap_store(tmp_path, monkeypatch, 1)  # ~1.3 MB store > 2 x 0.5 MB cap
    if last_prune_age_h is not None:
        (base / ".last_prune_success").write_text(str(time.time() - last_prune_age_h * 3600), encoding="utf-8")
    caplog.set_level(logging.ERROR, logger="tools.checkpoint_maintenance")
    assert checkpoint_maintenance.check_store_health(base, cap_mb=0.5) is True
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].startswith("checkpoint store over cap: "), errors
    assert ("last prune never" in errors[0]) == (last_prune_age_h is None)
    caplog.clear()
    assert checkpoint_maintenance.check_store_health(base, cap_mb=0.5) is False  # once a day
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_store_health_quiet_when_pruned_recently_or_under_cap(tmp_path, monkeypatch, caplog):
    base, _ = _over_cap_store(tmp_path, monkeypatch, 1)
    caplog.set_level(logging.ERROR, logger="tools.checkpoint_maintenance")
    assert checkpoint_maintenance.check_store_health(base, cap_mb=500) is False  # under the cap
    (base / ".last_health_check").unlink()  # independently exercise the recent-prune branch
    (base / ".last_prune_success").write_text(str(time.time() - 3600), encoding="utf-8")
    assert checkpoint_maintenance.check_store_health(base, cap_mb=1) is False  # <2x and pruned 1h ago
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_maintenance_reclaim_outlasts_three_tool_timeouts(tmp_path, monkeypatch):
    """The MacBook gc took longer than 3 x 30 s and was killed in its prune phase every run."""
    store = tmp_path / "store"
    store.mkdir()
    (store / GC_PENDING_NAME).touch()
    gc_seconds = 3 * checkpoints._GIT_TIMEOUT + 60

    def fake_git(args, _store, _cwd, timeout=checkpoints._GIT_TIMEOUT, **kwargs):
        if args[0] == "gc" and timeout < gc_seconds:
            return False, "", f"git timed out after {timeout}s"
        return True, "", ""

    tool_path = Pruner(fake_git, store, str(tmp_path), checkpoints._GIT_TIMEOUT, lambda _p: 0, "refs/hermes/")
    with pytest.raises(Exception):
        tool_path.reclaim()
    assert (store / GC_PENDING_NAME).exists()
    maintenance = Pruner(fake_git, store, str(tmp_path), checkpoints._GIT_TIMEOUT, lambda _p: 0, "refs/hermes/",
                         maintenance=True)
    maintenance.reclaim()
    assert not (store / GC_PENDING_NAME).exists()


def test_low_priority_git_runs_under_nice(tmp_path, monkeypatch):
    seen = []
    real_run = checkpoints.subprocess.run

    def spy(cmd, *args, **kwargs):
        seen.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(checkpoints.subprocess, "run", spy)
    ok, out, _ = checkpoints._run_git(["--version"], tmp_path, str(tmp_path), low_priority=True)
    assert ok and out.startswith("git version")
    assert seen[-1][1:3] == ["-n", "10"] and seen[-1][0].endswith("nice")


def test_enforce_size_gcs_log_times_not_once_per_project(tmp_path):
    """One gc per rewrite held the store lock for hours on a ~800-project store (each gc minutes)."""
    store = tmp_path / "store"
    store.mkdir()
    refs = {f"refs/hermes/{i:03d}": ["a", "b", "c"] for i in range(200)}
    state = {"gcs": 0, "rewrites": 0}

    def fake_git(args, _store, _cwd, **kwargs):
        verb = args[0]
        if verb == "for-each-ref":
            return True, "\n".join(refs), ""
        if verb == "rev-list":
            return True, "\n".join(refs[args[-1]]), ""
        if verb == "log":
            return True, "tree\x00date\x00date\x00msg", ""
        if verb == "commit-tree":
            return True, "new", ""
        if verb == "update-ref":
            refs[args[1]] = refs[args[1]][1:]
            state["rewrites"] += 1
        if verb == "gc":
            state["gcs"] += 1
        return True, "", ""

    # The store fits once 150 snapshots are gone (measured only after a gc).
    size = lambda _p: 0 if state["gcs"] > 1 and state["rewrites"] >= 150 else 10
    pruner = Pruner(fake_git, store, str(tmp_path), 30, size, "refs/hermes/", maintenance=True)
    assert pruner.enforce_size(5) is True
    assert state["gcs"] <= 10, state  # 1 initial + log2(150) batches, not one per rewrite
    assert state["rewrites"] < 300, state  # at most twice the needed drops
