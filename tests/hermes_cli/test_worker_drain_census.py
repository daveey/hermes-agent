"""Conservative census: no guessed baseline or empty-cmdline zero."""
from __future__ import annotations

import os

from hermes_cli import worker_drain_census as census


def _proc(root, pid, cgroup, cmdline='x\0', flags=0):
    d = root / str(pid)
    d.mkdir()
    (d / 'cgroup').write_text(f'0::{cgroup}\n')
    (d / 'cmdline').write_bytes(cmdline.encode())
    # stat fields 3 through 22: state, ppid, pgrp, session, tty, tpgid, flags, ... starttime
    fields = ['S', '1', '1', '1', '0', '-1', str(flags)] + ['0'] * 12 + ['100']
    (d / 'stat').write_text(f'{pid} (fixture process) ' + ' '.join(fields))
    return d


U = '/user.slice/user-1000.slice/user@1000.service'


def test_baseline_cgroup_never_exempts_arbitrary_children(tmp_path):
    _proc(tmp_path, 10, U + '/init.scope')
    _proc(tmp_path, 11, U + '/app.slice/gpg-agent.service')
    _proc(tmp_path, 12, U + '/app.slice/hermes-gateway.service')
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid) == 3


def test_unscoped_and_app_slice_work_counts(tmp_path):
    _proc(tmp_path, 10, U + '/init.scope')
    _proc(tmp_path, 11, U + '/app.slice/hermes-core-update.service')
    _proc(tmp_path, 12, '/user.slice/user-1000.slice/session-3.scope')
    _proc(tmp_path, 13, U + '/hermes.slice/hermes-kanban.slice/x.service')
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid) == 4


def test_empty_cmdline_is_not_kernel_identity(tmp_path):
    _proc(tmp_path, 10, '/', cmdline='')
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid) == 1


def test_kernel_flag_does_not_authorize_complete_zero(tmp_path):
    _proc(tmp_path, 10, '/', cmdline='', flags=0x200000)
    # Full host cgroup/namespace/manifest proof is not built yet.
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid) is None


def test_unreadable_entry_is_unknown(tmp_path):
    d = tmp_path / '10'
    d.mkdir()
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid) is None
    (d / 'cgroup').mkdir()
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid) is None


def test_missing_proc_is_unknown(tmp_path):
    assert census.census(str(tmp_path / 'nope')) is None
    assert census.census(str(tmp_path)) is None


def test_foreign_uid_unscoped_work_does_not_become_zero(tmp_path):
    _proc(tmp_path, 10, '/system.slice/amazon-ssm-agent.service')
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid + 1) is None


def test_non_v2_membership_holds(tmp_path):
    d = _proc(tmp_path, 10, '/')
    (d / 'cgroup').write_text('1:cpu:/legacy\n')
    assert census.census(str(tmp_path), uid=os.stat(tmp_path).st_uid) is None
