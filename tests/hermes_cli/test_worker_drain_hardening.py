"""Latch hardening (nofollow/owner/mode, root path, deadline) and the cron / shell-hook seams."""
from __future__ import annotations

import fcntl  # windows-footgun: ok
import json
import os
import time

import pytest

from hermes_cli import worker_drain_fence as f

IDENT = f.Identity('i-0123456789abcdef0', 'boot-1', 'epoch-1')


def req(op, gen, attempt='a1', **kw):
    now = int(time.time() * 1000)
    doc = {'version': 1, 'op': op, 'iid': IDENT.iid, 'boot': IDENT.boot, 'node_epoch': IDENT.node_epoch,
           'attempt': attempt, 'generation': gen, 'request_id': f'{op}-{gen}-{attempt}',
           'issued_at_ms': now, 'not_after_ms': now + 20000}
    doc.update(kw)
    return doc


@pytest.fixture
def root(tmp_path):
    f.install_state(tmp_path, IDENT)
    return tmp_path


def refuses(fn, *a, reason='unknown-state', **k):
    with pytest.raises(f.DrainRefused) as e:
        fn(*a, **k)
    assert e.value.reason == reason


def test_symlinked_latch_is_refused(root):
    latch = root / 'core-update' / f.LATCH_FILE
    real = root / 'elsewhere.json'
    real.write_bytes(latch.read_bytes())
    latch.unlink()
    latch.symlink_to(real)
    refuses(f.read_latch, root, IDENT)
    refuses(f.acquire_lease, root, IDENT, 'x')


def test_group_or_world_writable_latch_is_refused(root):
    latch = root / 'core-update' / f.LATCH_FILE
    assert f.read_latch(root, IDENT)['phase'] == 'open'
    latch.chmod(0o666)
    refuses(f.read_latch, root, IDENT)
    latch.chmod(0o620)
    refuses(f.read_latch, root, IDENT)
    latch.chmod(0o600)
    assert f.read_latch(root, IDENT)['phase'] == 'open'


def test_hardlinked_latch_is_refused(root):
    latch = root / 'core-update' / f.LATCH_FILE
    os.link(latch, root / 'alias.json')
    refuses(f.read_latch, root, IDENT)


def test_state_dir_must_be_a_real_private_directory(root, tmp_path):
    d = root / 'core-update'
    d.chmod(0o777)
    refuses(f.read_latch, root, IDENT)
    d.chmod(0o700)
    assert f.read_latch(root, IDENT)['phase'] == 'open'
    moved = root / 'moved'
    d.rename(moved)
    d.symlink_to(moved)
    refuses(f.read_latch, root, IDENT)


def test_install_adopts_the_group_writable_dir_the_update_fence_left(tmp_path):
    # Real workers: admission_fence created core-update under umask 0002 long before the first install.
    d = tmp_path / 'core-update'
    d.mkdir()
    d.chmod(0o2775)
    (d / f.LEASE_DIR).mkdir()
    (d / f.LEASE_DIR).chmod(0o777)
    f.install_state(tmp_path, IDENT)
    assert d.stat().st_mode & 0o7777 == 0o2755 and (d / f.LEASE_DIR).stat().st_mode & 0o777 == 0o755
    assert f.read_latch(tmp_path, IDENT)['phase'] == 'open'
    lease = f.acquire_lease(tmp_path, IDENT, 'x')
    assert f.busy_leases(tmp_path) == 1
    lease.close()
    f.install_state(tmp_path, IDENT)  # idempotent re-install over the adopted dir
    assert f.seal_precheck(tmp_path, IDENT) is True


def test_install_refuses_loudly_when_the_dir_cannot_be_trusted(tmp_path, monkeypatch):
    real = tmp_path / 'real'
    (real / 'core-update').mkdir(parents=True)
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'core-update').symlink_to(real / 'core-update')
    refuses(f.install_state, root, IDENT)  # symlinked state dir: never chmod through it
    assert not (real / 'core-update' / f.INSTALL_FILE).exists()
    other = tmp_path / 'other'
    (other / 'core-update').mkdir(parents=True)
    monkeypatch.setattr(f, '_trusted', lambda uid=None: {os.getuid() + 12345})  # windows-footgun: ok
    refuses(f.install_state, other, IDENT)  # foreign owner: not ours to tighten
    assert not (other / 'core-update' / f.INSTALL_FILE).exists()


def test_lease_dir_must_be_trusted_or_the_count_is_unknown(root):
    ld = root / 'core-update' / f.LEASE_DIR
    lease = f.acquire_lease(root, IDENT, 'x')
    ld.chmod(0o777)
    refuses(f.busy_leases, root)  # never a guessed zero
    refuses(f.acquire_lease, root, IDENT, 'y')
    refuses(f.seal_precheck, root, IDENT)
    ld.chmod(0o700)
    assert f.busy_leases(root) == 1
    lease.close()
    moved = root / 'moved-leases'
    ld.rename(moved)
    ld.symlink_to(moved)
    refuses(f.busy_leases, root)


def test_not_activated_or_not_posix_is_a_noop_never_an_error(tmp_path, monkeypatch):
    absent = tmp_path / 'absent.json'
    assert f.load_activation(absent) is None and f.admit('x', activation_path=absent) is None
    # Windows has neither flag; the module-level fallbacks keep the absent-file path working.
    monkeypatch.delattr(os, 'O_NOFOLLOW')
    monkeypatch.delattr(os, 'O_CLOEXEC')
    assert f.load_activation(absent) is None and f.admit('x', activation_path=absent) is None
    # Off POSIX (no fcntl) nothing can be enforced: even a present file is a no-op, not a refusal.
    present = tmp_path / 'act.json'
    present.write_text('not json', encoding='utf-8')
    monkeypatch.setattr(f, 'fcntl', None)
    assert f.load_activation(present) is None and f.admit('x', activation_path=present) is None


def test_census_reads_the_root_admit_writes_under(tmp_path, monkeypatch):
    from hermes_cli import worker_drain_census as census
    home = tmp_path / 'home'
    state = home / '.hermes'
    state.mkdir(parents=True)
    f.install_state(state, IDENT)
    act = {'uid': os.getuid(), 'home': str(home)}  # windows-footgun: ok
    monkeypatch.setattr(f, 'load_activation', lambda path=None: act)
    monkeypatch.setattr(f, '_state_base', lambda: tmp_path / 'roots-own-home')  # where a root heartbeat would look
    monkeypatch.setattr(f.os, 'geteuid', lambda: 0)
    lease = f.acquire_lease(state, f.Identity(IDENT.iid, IDENT.boot, IDENT.node_epoch, uid=act['uid']), 'x')
    assert f.census_root()[0] == state
    assert census.observe_leases() == census.Observation(census.POSITIVE, 1)
    lease.close()
    assert census.observe_leases() == census.Observation(census.ZERO)
    monkeypatch.setattr(f.os, 'geteuid', lambda: os.getuid() + 12345)  # windows-footgun: ok
    assert census.observe_leases().state == census.UNKNOWN  # foreign uid: refused, never zero


def test_foreign_owner_is_refused(root, monkeypatch):
    # Trust is {root, effective uid, activated uid}; a state file owned by anyone else is unknown.
    monkeypatch.setattr(f.os, 'geteuid', lambda: os.getuid() + 12345)  # windows-footgun: ok
    refuses(f.read_latch, root, IDENT)
    assert f.read_latch(root, f.Identity(IDENT.iid, IDENT.boot, IDENT.node_epoch, uid=os.getuid()))['phase'] == 'open'  # windows-footgun: ok


def test_activation_must_be_root_owned_by_default(tmp_path):
    path = tmp_path / 'act.json'
    path.write_text(json.dumps({'version': 1, 'iid': IDENT.iid, 'uid': 1000, 'node_epoch': 'e', 'home': '/home/ubuntu'}), encoding='utf-8')
    path.chmod(0o644)
    assert f.ACTIVATION_OWNERS == (0,)
    if os.getuid() != 0:  # windows-footgun: ok
        refuses(f.load_activation, path)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    refuses(f.load_activation, link)


def test_root_process_uses_the_activated_accounts_state_not_roots(monkeypatch):
    act = {'uid': 1000, 'home': '/home/ubuntu'}
    monkeypatch.setattr(f.os, 'geteuid', lambda: 0)
    assert str(f._activation_root(act)) == '/home/ubuntu/.hermes'
    monkeypatch.setattr(f.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(f, '_state_base', lambda: f.Path('/policy'))
    assert str(f._activation_root(act)) == '/policy'
    monkeypatch.setattr(f.os, 'geteuid', lambda: 4242)
    refuses(f._activation_root, act, reason='identity-mismatch')


def test_deadline_is_spent_by_lock_wait(root):
    fd = os.open(root / 'core-update' / f.LOCK_FILE, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another holder keeps the account lock
    try:
        now = int(time.time() * 1000)
        start = time.monotonic()
        refuses(f.apply_request, root, IDENT, req('establish', 1, issued_at_ms=now, not_after_ms=now + 300), now_ms=now)
        assert time.monotonic() - start < 1.5  # lock wait is bounded by the request deadline, not 2s
        assert f.read_latch(root, IDENT)['phase'] == 'open'
    finally:
        os.close(fd)


def test_expired_and_overlong_requests_refused(root):
    now = int(time.time() * 1000)
    refuses(f.apply_request, root, IDENT, req('establish', 1, issued_at_ms=now - 40000, not_after_ms=now - 1))
    refuses(f.apply_request, root, IDENT, req('establish', 1, issued_at_ms=now, not_after_ms=now + 60000))
    refuses(f.apply_request, root, IDENT, req('establish', 1, issued_at_ms=now + 60000, not_after_ms=now + 70000))
    assert f.apply_request(root, IDENT, req('establish', 1))['phase'] == 'draining'


# --------------------------------------------------------------------------- cron + shell hook seams

class _Lease:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_cron_registration_refused_while_draining_and_lease_released(monkeypatch):
    from cron import scheduler
    lease = _Lease()
    calls = []

    def admit(purpose, **kw):
        calls.append(purpose)
        if len(calls) == 1:
            raise f.DrainRefused('draining')
        return lease
    monkeypatch.setattr(f, 'admit', admit)
    monkeypatch.setattr(scheduler, '_running_job_ids', set())
    assert scheduler.try_register_running_job('drain-job-1') is False  # refused: nothing registered
    assert scheduler.get_running_job_ids() == frozenset()
    assert scheduler.try_register_running_job('drain-job-1') is True
    key = scheduler._inflight_key('drain-job-1')
    assert scheduler._running_leases[key] is lease and not lease.closed
    scheduler.release_running_job('drain-job-1')
    assert lease.closed and key not in scheduler._running_leases
    assert calls.count('cron.run') == 2


def test_shell_hook_refused_when_node_drains(monkeypatch):
    from agent import shell_hooks
    monkeypatch.setattr(f, 'admit', lambda purpose, **kw: (_ for _ in ()).throw(f.DrainRefused('sealed')))
    spec = shell_hooks.ShellHookSpec(event='pre_tool_call', command='/bin/true', timeout=5)
    out = shell_hooks._spawn(spec, '{}')
    assert out['returncode'] is None and 'sealed' in out['error']


def test_shell_hook_runs_when_not_activated():
    from agent import shell_hooks
    spec = shell_hooks.ShellHookSpec(event='pre_tool_call', command='/bin/true', timeout=5)
    assert shell_hooks._spawn(spec, '{}')['returncode'] == 0
