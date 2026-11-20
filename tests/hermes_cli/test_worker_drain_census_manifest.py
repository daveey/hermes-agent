"""Manifest census: zero only when every adapter proves it; sync-round probe; live baseline trust."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from hermes_cli import worker_drain_census as c
from hermes_cli import worker_drain_results_gate as gate
from hermes_cli.kanban_db_sync import SYNC_SCHEMA_SQL
from hermes_cli.kanban_sync_guard import board_sync_lock

ZERO, POS, UNK = c.Observation(c.ZERO), c.Observation(c.POSITIVE, 2), c.Observation(c.UNKNOWN, reason='x')
CG = '/system.slice/idle.service'


def adapters(a=ZERO, b=ZERO, r=ZERO):
    return {'host_processes': lambda: a, 'work_leases': lambda: b, 'local_results': lambda: r}


def test_zero_needs_every_adapter_and_unknown_never_becomes_zero():
    assert c.manifest_count(adapters()) == 0
    for position in range(3):
        mixed = [ZERO, ZERO, ZERO]
        mixed[position] = UNK
        assert c.manifest_count(adapters(*mixed)) is None
        mixed[position] = POS
        assert c.manifest_count(adapters(*mixed)) == 2


def test_adapter_fault_is_unknown_and_manifest_is_exact():
    def boom():
        raise RuntimeError('adapter down')
    assert c.manifest_count({**adapters(), 'work_leases': boom}) is None
    with pytest.raises(ValueError):
        c.observe_all({'host_processes': lambda: ZERO})
    with pytest.raises(ValueError):
        c.observe_all({**adapters(), 'extra': lambda: ZERO})


def _proc(root, pid, ppid=1, cgroup=CG, exe='/usr/sbin/idled', uid_dir=True, link=True):
    d = root / str(pid)
    d.mkdir()
    (d / 'cgroup').write_text(f'0::{cgroup}\n', encoding='utf-8')
    (d / 'cmdline').write_bytes(b'x\0')
    (d / 'stat').write_text(f'{pid} (p q) S {ppid} 1 1 0 -1 0 ' + ' '.join(['0'] * 12) + ' 100', encoding='utf-8')
    if link:
        os.symlink(exe, d / 'exe')


def _ident(root, cgroup=CG, exe='/usr/sbin/idled'):
    return (os.stat(root).st_uid, cgroup, exe)


def test_baseline_exempts_only_exact_identity_with_no_working_descendant(tmp_path):
    base = frozenset({_ident(tmp_path)})
    _proc(tmp_path, 10)
    assert c.observe_processes(str(tmp_path), base) == c.Observation(c.ZERO)
    _proc(tmp_path, 11, ppid=10, exe='/usr/bin/python3')  # unlisted descendant of an exempt daemon
    seen = c.observe_processes(str(tmp_path), base)
    assert seen.state == c.POSITIVE and seen.count == 2  # parent is dragged in: a unit name exempts no descendants


def test_same_exe_in_other_cgroup_is_work(tmp_path):
    base = frozenset({_ident(tmp_path)})
    _proc(tmp_path, 10, cgroup='/user.slice/job.scope')
    assert c.observe_processes(str(tmp_path), base).state == c.POSITIVE


def test_unlisted_process_without_baseline_is_work(tmp_path):
    _proc(tmp_path, 10)
    assert c.observe_processes(str(tmp_path), None).state == c.POSITIVE


def test_empty_proc_without_baseline_is_unknown(tmp_path):
    assert c.observe_processes(str(tmp_path), None) == c.Observation(c.UNKNOWN, reason='no trusted live baseline')
    assert c.observe_processes(str(tmp_path), frozenset()).state == c.ZERO


def test_unreadable_exe_blocks_zero(tmp_path):
    _proc(tmp_path, 10, link=False)  # alive, exe not readable (foreign uid)
    seen = c.observe_processes(str(tmp_path), frozenset({_ident(tmp_path)}))
    assert seen.state == c.UNKNOWN and 'unobservable' in seen.reason


def test_baseline_file_trust(tmp_path):
    path = tmp_path / 'b.json'
    path.write_text(json.dumps({'version': 1, 'idle': [{'uid': 0, 'cgroup': CG, 'exe': '/x'}]}), encoding='utf-8')
    path.chmod(0o644)
    assert c.load_baseline(str(path)) is None  # owned by the test user, not root
    assert c.load_baseline(str(tmp_path / 'missing')) is None
    link = tmp_path / 'l.json'
    link.symlink_to(path)
    assert c.load_baseline(str(link)) is None  # O_NOFOLLOW


def _board(tmp_path, name='kanban.db'):
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(SYNC_SCHEMA_SQL)
    conn.commit()
    conn.close()
    return path


def test_running_sync_round_is_not_clear_even_with_empty_outbox(tmp_path):
    path = _board(tmp_path)
    assert gate.local_results_state([path]).state == gate.CLEAR
    with board_sync_lock(path):
        state = gate.local_results_state([path])
        assert state.state == gate.PENDING and 'in flight' in state.reason
        assert gate.gate_count(0, [path]) is None
    assert gate.gate_count(0, [path]) == 0


def test_results_adapter_maps_states(monkeypatch):
    monkeypatch.setattr(gate, 'local_results_state', lambda boards=None: gate.ResultsState(gate.CLEAR))
    assert c.observe_results() == c.Observation(c.ZERO)
    monkeypatch.setattr(gate, 'local_results_state', lambda boards=None: gate.ResultsState(gate.PENDING, 3, 'r'))
    assert c.observe_results().count == 3
    monkeypatch.setattr(gate, 'local_results_state', lambda boards=None: gate.ResultsState(gate.UNKNOWN, reason='r'))
    assert c.observe_results().state == c.UNKNOWN
