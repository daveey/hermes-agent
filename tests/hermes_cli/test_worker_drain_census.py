"""Census: complete or UNKNOWN, over a fake /proc."""
from __future__ import annotations

import os

from hermes_cli import worker_drain_census as census


def _proc(root, pid, cgroup, cmdline="x\0", uid=None):
    d = root / str(pid)
    d.mkdir()
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "cmdline").write_bytes(cmdline.encode())
    (d / "status").write_text("State:\tS (sleeping)\n")
    return d


U = "/user.slice/user-1000.slice/user@1000.service"


def test_baseline_only_is_zero(tmp_path):
    _proc(tmp_path, 10, U + "/init.scope")
    _proc(tmp_path, 11, U + "/app.slice/gpg-agent.service")
    assert census.census(str(tmp_path), uid=os.stat(str(tmp_path)).st_uid) == 0


def test_unscoped_and_app_slice_work_counts(tmp_path):
    _proc(tmp_path, 10, U + "/init.scope")
    _proc(tmp_path, 11, U + "/app.slice/hermes-core-update.service")
    _proc(tmp_path, 12, "/user.slice/user-1000.slice/session-3.scope")
    _proc(tmp_path, 13, U + "/hermes.slice/hermes-kanban.slice/x.service")
    assert census.census(str(tmp_path), uid=os.stat(str(tmp_path)).st_uid) == 3


def test_kernel_thread_ignored(tmp_path):
    _proc(tmp_path, 10, "/", cmdline="")
    assert census.census(str(tmp_path), uid=os.stat(str(tmp_path)).st_uid) == 0


def test_unreadable_entry_is_unknown(tmp_path):
    d = tmp_path / "10"
    d.mkdir()  # no cgroup file readable -> exited => skipped
    assert census.census(str(tmp_path), uid=os.stat(str(tmp_path)).st_uid) == 0
    (d / "cgroup").mkdir()  # IsADirectoryError is an OSError we cannot classify
    assert census.census(str(tmp_path), uid=os.stat(str(tmp_path)).st_uid) is None


def test_missing_proc_is_unknown(tmp_path):
    assert census.census(str(tmp_path / "nope")) is None
    assert census.census(str(tmp_path)) is None
