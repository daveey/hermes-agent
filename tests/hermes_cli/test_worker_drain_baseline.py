"""Boot/instance and PID-reuse regressions for trusted drain baselines."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import worker_drain_census as c
from hermes_cli import worker_drain_fence as fence

pytestmark = pytest.mark.platforms('linux')
CG = '/system.slice/idle.service'


def process(root, pid=10, start=100, exe='/usr/sbin/idled'):
    d = root / str(pid)
    d.mkdir(exist_ok=True)
    fields = ['S', '1', '1', '1', '0', '-1', '0'] + ['0'] * 12 + [str(start)]
    (d / 'stat').write_text(f'{pid} (p q) ' + ' '.join(fields), encoding='ascii')
    (d / 'cgroup').write_text(f'0::{CG}\n', encoding='ascii')
    (d / 'cmdline').write_bytes(b'idled\0')
    if not (d / 'exe').is_symlink():
        (d / 'exe').symlink_to(exe)


@pytest.fixture
def host(monkeypatch, tmp_path):
    ident = fence.Identity('i-12345678', 'boot-a', 'epoch-a', os.getuid())
    monkeypatch.setattr(fence, 'load_activation', lambda: {'uid': os.getuid()})
    monkeypatch.setattr(fence, 'current_identity', lambda act: ident)
    from hermes_cli import worker_drain_baseline as producer
    monkeypatch.setattr(producer, 'validate_baseline', lambda base, ident: True)
    path = tmp_path / 'baseline.json'
    # Model a root-installed file without requiring privileged pytest. The real
    # descriptor checks remain exercised in the trust test below.
    real_fstat = c.os.fstat
    def root_stat(fd):
        st = real_fstat(fd)
        return SimpleNamespace(st_uid=0, st_mode=st.st_mode, st_nlink=st.st_nlink)
    monkeypatch.setattr(c.os, 'fstat', root_stat)
    doc = {'version': 2, 'boot_id': ident.boot, 'iid': ident.iid, 'node_epoch': ident.node_epoch,
           'manifest_digest': 'trusted-test-digest',
           'idle': [{'pid': 10, 'start_ticks': 100, 'uid': os.getuid(), 'cgroup': CG, 'exe': '/usr/sbin/idled'}]}
    def load(**changes):
        path.write_text(json.dumps({**doc, **changes}), encoding='utf-8')
        path.chmod(0o644)
        return c.load_baseline(str(path))
    return tmp_path, path, load


def test_fresh_idle_and_same_executable_sibling(host):
    root, _, load = host
    process(root)
    baseline = load()
    assert baseline is not None
    assert c.observe_processes(str(root), baseline).state == c.ZERO
    process(root, pid=11)
    assert c.observe_processes(str(root), baseline).state == c.POSITIVE


def test_pid_reuse_is_not_idle(host):
    root, _, load = host
    process(root, start=101)
    assert c.observe_processes(str(root), load()).state == c.POSITIVE


@pytest.mark.parametrize('changes', [{'boot_id': 'old-boot'}, {'iid': 'i-87654321'},
                                    {'node_epoch': 'old-epoch'}, {'version': 1}])
def test_stale_or_legacy_baseline_unknown(host, changes):
    root, _, load = host
    assert load(**changes) is None
    assert c.observe_processes(str(root), c.load_baseline(str(root / 'missing'))).state == c.UNKNOWN


def test_binding_revalidated_after_load(host, monkeypatch):
    root, _, load = host
    process(root)
    baseline = load()
    monkeypatch.setattr(fence, 'current_identity', lambda act: fence.Identity('i-12345678', 'boot-b', 'epoch-a'))
    assert c.observe_processes(str(root), baseline).state == c.UNKNOWN


@pytest.mark.parametrize('entry', [{'pid': True}, {'pid': -1}, {'start_ticks': 0}, {'uid': '0'}, {'cgroup': ''}])
def test_malformed_identity_never_exempts(host, entry):
    root, _, load = host
    process(root)
    idle = {'pid': 10, 'start_ticks': 100, 'uid': os.getuid(), 'cgroup': CG, 'exe': '/usr/sbin/idled'}
    assert load(idle=[{**idle, **entry}]) is None


def test_unsafe_mode_and_links_refused(host):
    _, path, load = host
    assert load() is not None
    path.chmod(0o666)
    assert c.load_baseline(str(path)) is None
    path.chmod(0o644)
    hard = path.with_name('hard')
    hard.hardlink_to(path)
    assert c.load_baseline(str(path)) is None
    hard.unlink()
    link = path.with_name('link')
    link.symlink_to(path)
    assert c.load_baseline(str(link)) is None
