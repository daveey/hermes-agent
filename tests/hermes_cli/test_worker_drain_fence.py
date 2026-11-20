import json
import os
import multiprocessing
import time

import pytest

from hermes_cli import worker_drain_fence as f

IDENT = f.Identity('i-0123456789abcdef0', 'boot-1', 'epoch-1')


def req(op, gen, attempt='a1', rid=None, ident=IDENT, **kw):
    now = int(time.time() * 1000)
    doc = {'version': 1, 'op': op, 'iid': ident.iid, 'boot': ident.boot, 'node_epoch': ident.node_epoch,
           'attempt': attempt, 'generation': gen, 'request_id': rid or f'{op}-{gen}-{attempt}',
           'issued_at_ms': now, 'not_after_ms': now + 20000}
    doc.update(kw)
    return doc


@pytest.fixture
def root(tmp_path):
    f.install_state(tmp_path, IDENT)
    return tmp_path


def refuses(reason, fn, *a, **k):
    with pytest.raises(f.DrainRefused) as e:
        fn(*a, **k)
    assert e.value.reason == reason


def test_open_admits_and_lease_busy_while_held(root):
    lease = f.acquire_lease(root, IDENT, 'test')
    assert f.busy_leases(root) == 1
    lease.close()
    assert f.busy_leases(root) == 0
    assert lease.path.exists()  # close never unlinks


def test_late_spawn_after_latch_refused_early_lease_holds_across_seal_check(root):
    early = f.acquire_lease(root, IDENT, 'early')
    f.apply_request(root, IDENT, req('establish', 1))
    refuses('draining', f.acquire_lease, root, IDENT, 'late')
    assert f.seal_precheck(root, IDENT) is False  # early lease still counted
    child = f.acquire_lease(root, IDENT, 'descendant', parent=early)  # held parent permits descendants
    child.close()
    early.close()
    assert f.seal_precheck(root, IDENT) is True
    f.apply_request(root, IDENT, req('seal', 1))
    refuses('sealed', f.acquire_lease, root, IDENT, 'x')
    refuses('sealed', f.acquire_lease, root, IDENT, 'x', parent=early)


def test_exact_replay_returns_receipt_and_changed_bytes_refuse(root):
    r = req('establish', 1)
    first = f.apply_request(root, IDENT, r)
    assert f.apply_request(root, IDENT, r) == first
    refuses('identity-mismatch', f.apply_request, root, IDENT, dict(r, issued_at_ms=r['issued_at_ms'] + 1))


def test_generation_rules_and_cancel(root):
    f.apply_request(root, IDENT, req('establish', 2))
    refuses('identity-mismatch', f.apply_request, root, IDENT, req('establish', 1, 'a0'))
    refuses('identity-mismatch', f.apply_request, root, IDENT, req('cancel', 2, 'other'))
    refuses('draining', f.apply_request, root, IDENT, req('establish', 3, 'a3'))
    assert f.apply_request(root, IDENT, req('cancel', 2))['phase'] == 'cancelled'
    assert f.apply_request(root, IDENT, req('establish', 3, 'a3'))['phase'] == 'draining'
    f.apply_request(root, IDENT, req('seal', 3, 'a3'))
    refuses('identity-mismatch', f.apply_request, root, IDENT, req('cancel', 3, 'a3'))  # no cancel after seal


def test_closed_parent_cannot_admit_descendant(root):
    parent = f.acquire_lease(root, IDENT, 'parent')
    parent.close()
    f.apply_request(root, IDENT, req('establish', 1))
    refuses('draining', f.acquire_lease, root, IDENT, 'late', parent=parent)


def test_open_latch_from_previous_boot_is_unknown(root):
    other = f.Identity(IDENT.iid, 'boot-2', IDENT.node_epoch)
    refuses('unknown-state', f.acquire_lease, root, other, 'late')


def test_bad_request_shapes(root):
    refuses('unknown-state', f.apply_request, root, IDENT, req('establish', 1, extra=1))
    refuses('identity-mismatch', f.apply_request, root, IDENT, req('establish', 1, ident=f.Identity(IDENT.iid, 'boot-2', 'epoch-1')))
    old = int(time.time() * 1000) - 60000
    refuses('unknown-state', f.apply_request, root, IDENT, req('establish', 1, issued_at_ms=old, not_after_ms=old + 1000))


def test_missing_corrupt_other_boot_never_open(root):
    f.apply_request(root, IDENT, req('establish', 1))
    refuses('unknown-state', f.acquire_lease, root, f.Identity(IDENT.iid, 'boot-2', IDENT.node_epoch), 'x')
    refuses('identity-mismatch', f.acquire_lease, root, f.Identity('i-ffffffff', 'boot-1', 'epoch-1'), 'x')
    (root / 'core-update' / f.LATCH_FILE).write_text('{bad', encoding='utf-8')
    refuses('unknown-state', f.acquire_lease, root, IDENT, 'x')
    (root / 'core-update' / f.LATCH_FILE).unlink()
    refuses('unknown-state', f.acquire_lease, root, IDENT, 'x')


def test_activation(tmp_path, monkeypatch):
    monkeypatch.setattr(f, 'ACTIVATION_OWNERS', (os.geteuid(),))  # windows-footgun: ok
    path = tmp_path / 'act.json'
    assert f.load_activation(path) is None
    path.write_text('{nope', encoding='utf-8')
    refuses('unknown-state', f.load_activation, path)
    path.write_text(json.dumps({'version': 1, 'iid': IDENT.iid, 'uid': 1000, 'node_epoch': 'e', 'home': '/home/ubuntu'}), encoding='utf-8')
    path.chmod(0o644)
    assert f.load_activation(path)['uid'] == 1000


def _hold(root, ready, done):
    lease = f.acquire_lease(root, IDENT, 'child')
    ready.set()
    done.wait(10)
    lease.close()


def test_lease_busy_across_processes_and_released_on_exit(root):
    ctx = multiprocessing.get_context('fork')
    ready, done = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(root, ready, done))
    proc.start()
    assert ready.wait(10)
    assert f.busy_leases(root) == 1
    f.apply_request(root, IDENT, req('establish', 1))
    assert f.seal_precheck(root, IDENT) is False
    done.set()
    proc.join(10)
    assert f.busy_leases(root) == 0
