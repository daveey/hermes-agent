"""Scratch archives are removed past their retention, and oldest-first under disk pressure. [daveey]

worker-a2, 2026-11-23: `kanban gc` archived every aged scratch tree into
``archive/scratch/<board>/<task>`` on the same disk and nothing removed those archives, so 403 of them
(62 GB, three days) filled the disk toward the dispatcher's free-space floor.
"""
import logging
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_scratch_archive_retention as ret
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers
from tests.hermes_cli.test_kanban_scratch_gc_archive import _archive, _gc, _task

board = helpers.board
remote = helpers.remote

NOW = 2_000_000_000.0
DAY = 86400
GIB = 1 << 30


@pytest.fixture
def root(tmp_path):
    # Not tmp_path itself: the test Hermes home lives there too.
    out = tmp_path / "archive-scratch"
    out.mkdir()
    return out


def _cfg(monkeypatch, **kanban):
    monkeypatch.setattr(ret, "_kanban_doc", lambda: kanban)


def _make(root, name, age_s, board_slug="default", now=NOW):
    path = root / board_slug / name
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "meta.json").write_text("{}")
    for p in (path / "repo" / "meta.json", path / "repo", path):
        os.utime(p, (now - age_s, now - age_s))
    return path


def test_age_rule_removes_only_archives_past_retention(root, monkeypatch):
    _cfg(monkeypatch)  # defaults: 7 days, no floor
    old = _make(root, "t_old", 8 * DAY)
    other_board = _make(root, "t_old2", 30 * DAY, board_slug="ops")
    young = _make(root, "t_young", 6 * DAY)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 2
    assert not old.exists() and not other_board.exists()
    assert young.is_dir()


def test_configured_days_and_zero(root, monkeypatch):
    a = _make(root, "t_a", 3 * DAY)
    _cfg(monkeypatch, scratch_archive_retention_days=0)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 0
    assert a.is_dir()
    _cfg(monkeypatch, scratch_archive_retention_days=2.0)  # YAML 2.0 is two whole days
    assert ret.prune_scratch_archives(root=root, now=NOW) == 1
    assert not a.exists()


def test_age_runs_from_the_newest_entry_two_levels_down(root, monkeypatch):
    """A build still writing its payload, or an archive whose detach intent just landed, is not old."""
    _cfg(monkeypatch, admission={"disk_free_floor_bytes": GIB})
    monkeypatch.setattr(ret, "_free_bytes", lambda _p: 0)
    writing = _make(root, "t_w", 30 * DAY)
    os.utime(writing / "repo" / "meta.json", (NOW - 60, NOW - 60))  # grandchild written a minute ago
    building = _make(root, "t_b.partial-999999997", 30 * DAY)
    os.utime(building / "repo", (NOW - 60, NOW - 60))
    deep = _make(root, "t_d", 30 * DAY)
    (deep / "repo" / "sub").mkdir()
    (deep / "repo" / "sub" / "f").write_text("x")  # three levels down: not looked at
    for p in (deep / "repo" / "sub", deep / "repo", deep):
        os.utime(p, (NOW - 30 * DAY,) * 2)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 1
    assert writing.is_dir() and building.is_dir() and not deep.exists()


@pytest.mark.parametrize("bad", [{"scratch_archive_retention_days": -1},
                                 {"scratch_archive_retention_days": "7"},
                                 {"scratch_archive_retention_days": True},
                                 {"scratch_archive_retention_days": 0.01},
                                 {"scratch_archive_pressure_min_age_hours": 0},
                                 {"scratch_archive_pressure_min_age_hours": 0.5},
                                 {"scratch_archive_pressure_min_age_hours": float("nan")},
                                 {"admission": {"disk_free_floor_bytes": 0}},
                                 {"admission": {"disk_free_floor_bytes": 1.5}}])
def test_invalid_policy_removes_nothing(root, monkeypatch, caplog, bad):
    a = _make(root, "t_a", 100 * DAY)
    _cfg(monkeypatch, **bad)
    monkeypatch.setattr(ret, "_free_bytes", lambda _p: 0)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 0
    assert a.is_dir()
    assert "keeping every archive" in caplog.text


def test_policy_is_read_from_the_installation_root_config(monkeypatch, tmp_path):
    from hermes_constants import get_default_hermes_root
    profile = tmp_path / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))  # a profile's own home: its config must not decide
    (profile / "config.yaml").write_text("kanban:\n  scratch_archive_retention_days: 99\n")
    root_cfg = Path(get_default_hermes_root()) / "config.yaml"
    assert root_cfg.parent != profile
    root_cfg.write_text("kanban:\n  scratch_archive_retention_days: 3\n"
                        "  scratch_archive_pressure_min_age_hours: 2\n"
                        "  admission:\n    disk_free_floor_bytes: 1024\n")
    assert ret.settings() == (3, 2.0, 1024)
    root_cfg.unlink()
    assert ret.settings() == (7, 24.0, None)


def test_symlinks_files_and_detach_intent_are_never_removed(root, monkeypatch, caplog):
    _cfg(monkeypatch, scratch_archive_retention_days=1)
    intent = _make(root, "t_i", 30 * DAY)
    (intent / "detach-abc.json").write_text("{}")
    os.utime(intent, (NOW - 30 * DAY,) * 2)
    target = root.parent / "outside"
    (target / "t_inner" / "repo").mkdir(parents=True)  # would be an old archive if links were followed
    os.utime(target / "t_inner", (NOW - 30 * DAY,) * 2)
    link = root / "default" / "t_link"
    link.symlink_to(target)
    stray = root / "default" / "t_file"
    stray.write_text("x")
    (root / "linked-board").symlink_to(target)
    caplog.set_level(logging.WARNING)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 0
    assert intent.is_dir() and stray.is_file()
    assert link.is_symlink() and (target / "t_inner" / "repo").is_dir()
    assert "holds a detach recovery intent" in caplog.text


def test_partial_builds_live_kept_dead_and_old_removed(root, monkeypatch):
    _cfg(monkeypatch)
    live = _make(root, f"t_l.partial-{os.getpid()}", 30 * DAY)
    dead_old = _make(root, "t_d.partial-999999999", 25 * 3600)
    dead_young = _make(root, "t_y.partial-999999998", 3600)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 1
    assert live.is_dir() and dead_young.is_dir() and not dead_old.exists()


@pytest.mark.parametrize("shape", ["unreadable", "file"])
def test_unreadable_root_is_logged_and_gc_goes_on(root, monkeypatch, caplog, shape):
    _cfg(monkeypatch, admission={"disk_free_floor_bytes": GIB})
    _make(root, "t_a", 30 * DAY)
    if shape == "file":
        target = root.parent / "root-file"
        target.write_text("x")
    else:
        target = root
        monkeypatch.setattr(ret.os, "scandir", _denied(ret.os.scandir, target))
    caplog.set_level(logging.WARNING)
    assert ret.prune_scratch_archives(root=target, now=NOW) == 0
    assert "removing nothing" in caplog.text


def _denied(real, blocked):
    def scandir(path="."):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)
    return scandir


def test_statvfs_failure_stops_only_the_pressure_rule(root, monkeypatch, caplog):
    _cfg(monkeypatch, admission={"disk_free_floor_bytes": GIB})
    old = _make(root, "t_old", 8 * DAY)
    young = _make(root, "t_young", 2 * DAY)

    def broken(_p):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(ret, "_free_bytes", broken)
    caplog.set_level(logging.WARNING)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 1
    assert not old.exists() and young.is_dir()
    assert "cannot measure free space" in caplog.text


def test_missing_root_is_a_no_op(root, monkeypatch):
    _cfg(monkeypatch, admission={"disk_free_floor_bytes": GIB})
    monkeypatch.setattr(ret, "_free_bytes", lambda _p: 0)
    assert ret.prune_scratch_archives(root=root / "absent", now=NOW) == 0


class _Disk:
    """Free space that grows by each removed archive's size (GiB per archive)."""

    def __init__(self, free_gib, sizes):
        self.free = free_gib * GIB
        self.sizes = sizes

    def install(self, monkeypatch):
        real = ret._remove

        def remove(path, why, age):
            ok = real(path, why, age)
            if ok:
                self.free += self.sizes[path.name] * GIB
            return ok
        monkeypatch.setattr(ret, "_remove", remove)
        monkeypatch.setattr(ret, "_free_bytes", lambda _p: self.free)


def test_pressure_removes_oldest_first_until_three_floors_free(root, monkeypatch):
    _cfg(monkeypatch, admission={"disk_free_floor_bytes": 10 * GIB})
    ages = {"t_1": 6 * DAY, "t_2": 5 * DAY, "t_3": 4 * DAY, "t_4": 3 * DAY, "t_5": 2 * DAY}
    paths = {n: _make(root, n, a) for n, a in ages.items()}
    disk = _Disk(15, dict.fromkeys(ages, 6))
    disk.install(monkeypatch)
    # 15 -> 21 -> 27 -> 33 GiB: stops once 30 GiB (3 x floor) is free.
    assert ret.prune_scratch_archives(root=root, now=NOW) == 3
    assert [n for n, p in paths.items() if p.exists()] == ["t_4", "t_5"]


def test_no_pressure_above_two_floors(root, monkeypatch):
    _cfg(monkeypatch, admission={"disk_free_floor_bytes": 10 * GIB})
    a = _make(root, "t_a", 6 * DAY)
    _Disk(20, {"t_a": 6}).install(monkeypatch)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 0
    assert a.is_dir()


def test_pressure_keeps_archives_younger_than_min_age(root, monkeypatch, caplog):
    _cfg(monkeypatch, admission={"disk_free_floor_bytes": 10 * GIB}, scratch_archive_pressure_min_age_hours=24)
    old = _make(root, "t_old", 2 * DAY)
    fresh = _make(root, "t_fresh", 23 * 3600)
    _Disk(1, {"t_old": 1, "t_fresh": 50}).install(monkeypatch)
    caplog.set_level(logging.WARNING)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 1
    assert not old.exists() and fresh.is_dir()
    assert "after pruning" in caplog.text


def test_no_floor_means_no_pressure_rule(root, monkeypatch):
    _cfg(monkeypatch)
    a = _make(root, "t_a", 6 * DAY)
    monkeypatch.setattr(ret, "_free_bytes", lambda _p: 0)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 0
    assert a.is_dir()


def test_free_bytes_reads_the_real_filesystem(root):
    st = os.statvfs(root)
    assert ret._free_bytes(root) == st.f_bavail * st.f_frsize > 0


def test_failed_removal_is_logged_and_the_pass_continues(root, monkeypatch, caplog):
    _cfg(monkeypatch)
    a = _make(root, "t_a", 9 * DAY)
    b = _make(root, "t_b", 8 * DAY)
    real = ret.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if os.path.basename(path) == "t_a":
            raise PermissionError(13, "denied")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(ret.shutil, "rmtree", rmtree)
    caplog.set_level(logging.WARNING)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 1
    assert a.is_dir() and not b.exists()
    assert "could not remove" in caplog.text


def test_kanban_gc_prunes_an_expired_archive_it_made_earlier(board, remote, capsys):
    tid, path = _task(remote=remote)
    assert _gc() == 0
    archive = _archive(tid)
    assert archive.is_dir() and not path.exists()
    assert capsys.readouterr().out.splitlines()[-1].endswith(", 0 scratch archive(s) removed")
    old = time.time() - 8 * DAY
    for top, dirs, files in os.walk(archive, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(top, name), (old, old), follow_symlinks=False)
    os.utime(archive, (old, old))
    assert _gc() == 0
    assert not archive.exists()
    assert capsys.readouterr().out.splitlines()[-1].endswith(", 1 scratch archive(s) removed")
