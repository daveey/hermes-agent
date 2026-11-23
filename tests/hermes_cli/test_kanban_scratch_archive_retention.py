"""Scratch archives are removed past their retention, and oldest-first under disk pressure. [daveey]

worker-a2, 2026-11-23: `kanban gc` archived every aged scratch tree into
``archive/scratch/<board>/<task>`` on the same disk and nothing removed those archives, so 403 of them
(62 GB, three days) filled the disk toward the dispatcher's free-space floor.
"""
import logging
import os
import time

import pytest

from hermes_cli import config as hconfig
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
    monkeypatch.setattr(hconfig, "load_config", lambda: {"kanban": kanban})


def _make(root, name, age_s, board_slug="default", now=NOW):
    path = root / board_slug / name
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "meta.json").write_text("{}")
    os.utime(path, (now - age_s, now - age_s))
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
    _cfg(monkeypatch, scratch_archive_retention_days=2)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 1
    assert not a.exists()


@pytest.mark.parametrize("bad", [{"scratch_archive_retention_days": -1},
                                 {"scratch_archive_retention_days": "7"},
                                 {"scratch_archive_retention_days": True},
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


def test_partial_symlink_file_and_detach_intent_are_never_removed(root, monkeypatch, caplog):
    _cfg(monkeypatch, scratch_archive_retention_days=1)
    partial = _make(root, "t_p.partial-123", 30 * DAY)
    intent = _make(root, "t_i", 30 * DAY)
    (intent / "detach-abc.json").write_text("{}")
    os.utime(intent, (NOW - 30 * DAY,) * 2)
    target = root.parent / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = root / "default" / "t_link"
    link.symlink_to(target)
    stray = root / "default" / "t_file"
    stray.write_text("x")
    board_link = root / "linked-board"
    board_link.symlink_to(target)
    caplog.set_level(logging.WARNING)
    assert ret.prune_scratch_archives(root=root, now=NOW) == 0
    assert partial.is_dir() and intent.is_dir() and stray.is_file()
    assert link.is_symlink() and (target / "keep.txt").is_file()
    assert "holds a detach recovery intent" in caplog.text


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


def test_failed_removal_is_logged_and_the_pass_continues(root, monkeypatch, caplog):
    _cfg(monkeypatch)
    a = _make(root, "t_a", 9 * DAY, now=NOW)
    b = _make(root, "t_b", 8 * DAY, now=NOW)
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


def test_kanban_gc_prunes_an_expired_archive_it_made_earlier(board, remote, capsys, monkeypatch):
    tid, path = _task(remote=remote)
    assert _gc() == 0
    archive = _archive(tid)
    assert archive.is_dir() and not path.exists()
    assert capsys.readouterr().out.splitlines()[-1].endswith(", 0 scratch archive(s) removed")
    old = time.time() - 8 * DAY
    os.utime(archive, (old, old))
    assert _gc() == 0
    assert not archive.exists()
    assert capsys.readouterr().out.splitlines()[-1].endswith(", 1 scratch archive(s) removed")
