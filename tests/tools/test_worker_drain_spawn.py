"""Admission/spawn hooks honour the drain latch (isolated root, no AWS, no live node)."""
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_cli import worker_drain_fence as f

IID = 'i-0123456789abcdef0'


@pytest.fixture
def node(tmp_path, monkeypatch):
    act = tmp_path / 'activation.json'
    root = tmp_path / 'acct'
    root.mkdir()
    ident = f.Identity(IID, f.read_boot_id(), 'epoch-1')
    f.install_state(root, ident)
    act.write_text(json.dumps({'version': 1, 'iid': IID, 'uid': os.getuid(), 'node_epoch': 'epoch-1',
                               'home': str(tmp_path)}))
    monkeypatch.setattr(f, 'ACTIVATION_PATH', act)
    monkeypatch.setattr(f, '_state_base', lambda: root)
    monkeypatch.setattr(f, 'read_imds_iid', lambda timeout=1.5: IID)
    monkeypatch.delenv(f.LEASE_FD_ENV, raising=False)
    f._iid_cache.clear()
    return root, ident


def latch(root, ident, op='establish', gen=1):
    now = int(time.time() * 1000)
    return f.apply_request(root, ident, {
        'version': 1, 'op': op, 'iid': ident.iid, 'boot': ident.boot, 'node_epoch': ident.node_epoch,
        'attempt': 'a1', 'generation': gen, 'request_id': f'{op}-{gen}', 'issued_at_ms': now,
        'not_after_ms': now + 20000})


def test_not_activated_is_a_full_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(f, 'ACTIVATION_PATH', tmp_path / 'absent.json')
    assert f.admit('x') is None

    @f.fenced('t')
    def go():
        return 7
    assert go() == 7


def test_fenced_hook_refuses_after_latch_and_admits_before(node):
    root, ident = node
    calls = []

    @f.fenced('t')
    def go():
        calls.append(f.busy_leases(root))  # lease held during the call
        return 'ok'
    assert go() == 'ok' and calls == [1] and f.busy_leases(root) == 0
    latch(root, ident)
    with pytest.raises(f.DrainRefused) as e:
        go()
    assert e.value.reason == 'draining' and calls == [1]


def test_unknown_state_and_wrong_iid_refuse(node, monkeypatch):
    root, _ident = node
    monkeypatch.setattr(f, 'read_imds_iid', lambda timeout=1.5: 'i-0fffffffffffffff0')
    f._iid_cache.clear()
    with pytest.raises(f.DrainRefused) as e:
        f.admit('x')
    assert e.value.reason == 'identity-mismatch'
    monkeypatch.setattr(f, 'read_imds_iid', lambda timeout=1.5: IID)
    f._iid_cache.clear()
    (root / 'core-update' / f.LATCH_FILE).unlink()
    with pytest.raises(f.DrainRefused) as e:
        f.admit('x')
    assert e.value.reason == 'unknown-state'


def test_real_spawn_hooks_refuse_when_latched(node):
    root, ident = node
    from hermes_cli.process_identity import spawn_owned
    from tools.process_registry import ProcessRegistry
    p = spawn_owned([sys.executable, '-c', 'pass'], purpose='drain-test')
    p.wait()
    latch(root, ident)
    with pytest.raises(f.DrainRefused):
        spawn_owned([sys.executable, '-c', 'pass'], purpose='drain-test')
    with pytest.raises(f.DrainRefused):
        ProcessRegistry().spawn_local('true')
    with pytest.raises(f.DrainRefused):
        ProcessRegistry().spawn_via_env(object(), 'true')


def test_worker_lease_survives_dispatcher_and_admits_descendant(node):
    root, ident = node
    lease = f.admit('kanban.worker')
    child = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'], pass_fds=lease.pass_fds(),
        env=lease.child_env())
    try:
        lease.close()
        assert f.busy_leases(root) == 1  # the worker keeps the flock after the spawner closed
        latch(root, ident)
        assert f.seal_precheck(root, ident) is False
    finally:
        child.kill()
        child.wait()
    assert f.seal_precheck(root, ident) is True


def test_descendant_with_inherited_parent_admitted_while_draining(node, monkeypatch):
    root, ident = node
    parent = f.admit('kanban.worker')
    latch(root, ident)
    monkeypatch.setenv(f.LEASE_FD_ENV, str(parent.fd))
    kid = f.admit('terminal')
    assert kid is not None
    kid.close()
    monkeypatch.setenv(f.LEASE_FD_ENV, '999')  # bogus fd never mints authority
    with pytest.raises(f.DrainRefused):
        f.admit('terminal')
    parent.close()


def test_claim_transaction_defers_when_latched(node):
    from hermes_cli.kanban_admission import claim_transaction
    root, ident = node
    latch(root, ident)
    conn = sqlite3.connect(':memory:')
    with claim_transaction(conn, task_id='t_x') as admitted:
        assert admitted is False
    assert not conn.in_transaction
