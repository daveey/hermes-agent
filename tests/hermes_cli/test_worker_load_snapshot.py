"""Sanitized, birth-fenced interval CPU evidence and escaped units."""
import json
import os

from hermes_cli import worker_load_snapshot as snap
from hermes_cli import worker_compute_capacity as capacity


def _stat(proc, pid, ticks, group, comm='python', birth=99):
    path = proc / str(pid)
    path.mkdir(exist_ok=True)
    fields = ['S'] + ['0'] * 19
    fields[11], fields[12], fields[19] = str(ticks), '0', str(birth)
    (path / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields))
    (path / 'cgroup').write_text('0::' + group + '\n')


def test_interval_top_owners_escapes_and_sanitization(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    root = tmp_path / 'home'
    owners = root / 'worker-resources/owners'
    owners.mkdir(parents=True)
    monkeypatch.setattr(capacity, 'policy_root', lambda: root)
    owner = ['/installation/kanban.db', 't_parked']
    name = capacity.owner_slice(*owner)
    (owners / f'{name}.json').write_text(json.dumps(owner))
    group = '/user.slice/hermes.slice/hermes-kanban.slice/' + name + '/job.scope'
    _stat(proc, 100, 10, group, comm='py[evil]`')
    _stat(proc, 101, 10, '/user.slice/app.slice/escape.service')
    _stat(proc, 102, 10, group)
    def advance(_):
        _stat(proc, 100, 210, group, comm='py[evil]`')
        _stat(proc, 101, 110, '/user.slice/app.slice/escape.service')
        _stat(proc, 102, 99999, group, birth=100)  # PID reuse cannot claim old CPU
    monkeypatch.setattr(snap.time, 'sleep', advance)
    clock = iter([0.0, 1.0])
    monkeypatch.setattr(snap.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(os, 'sysconf', lambda _: 100)
    evidence = snap.snapshot(proc=proc)
    assert evidence['unknown_processes'] == 1
    assert evidence['processes'][0] == dict(pid=100, comm='py_evil__', cpu_percent=200.0,
                                            card='t_parked', contained=True, uncontained_same_uid=False)
    assert evidence['uncontained_active'][0]['pid'] == 101
    assert evidence['uncontained_active'][0]['card'] == 'unknown'
    assert '/installation' not in json.dumps(evidence)
    assert 'app.slice' not in json.dumps(evidence)


def test_bad_owner_map_is_unknown_not_misattributed(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    owners = tmp_path / 'worker-resources/owners'
    owners.mkdir(parents=True)
    (owners / 'wrong.json').write_text(json.dumps(['/db/kanban.db', 't_other']))
    malformed = ['/db/kanban.db', 5]
    (owners / (capacity.owner_slice(*malformed) + '.json')).write_text(json.dumps(malformed))
    evidence = snap.snapshot(proc=tmp_path)
    assert evidence['owner_map'] == 'partial'
    assert evidence['owner_errors'] == 2
    assert evidence['processes'] == []
