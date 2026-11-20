"""Update/turn reservation drain coverage, with actual isolated flock state."""
import pytest

from hermes_cli import admission_fence as admission
from hermes_cli import worker_drain_fence as f
from tests.tools import test_worker_drain_spawn as fixtures

node = fixtures.node
latch = fixtures.latch


@pytest.mark.parametrize('method', [admission.reserve, admission.extend])
def test_reservation_holds_drain_lease_until_close(node, method):
    root, ident = node
    reservation = method(root)
    assert reservation is not None
    try:
        assert f.busy_leases(root) == 1
        latch(root, ident)
        assert not f.seal_precheck(root, ident)
    finally:
        reservation.close()
    reservation.close()
    assert f.seal_precheck(root, ident)


@pytest.mark.parametrize('method', [admission.reserve, admission.extend])
def test_reservation_after_latch_refuses(node, method):
    root, ident = node
    latch(root, ident)
    with pytest.raises(f.DrainRefused):
        method(root)
    assert f.busy_leases(root) == 0


def test_turn_callback_retains_authority_across_thread_handoff(node, monkeypatch):
    import subprocess
    import sys
    import threading
    import hermes_constants
    from tui_gateway import turn_admission as turns
    root, ident = node
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda **kw: root)
    token = turns.acquire(root)
    assert token is not None
    session = {'history_lock': threading.RLock(), '_turn_admission': token}
    outputs = []

    def target():
        proc = f.popen_worker(subprocess.Popen,
            [sys.executable, '-c', 'print("finished")'], stdout=subprocess.PIPE)
        output, _ = proc.communicate(timeout=10)
        outputs.append(output)
    callback, _cancel = turns.retained_callback(session, target)
    token.release()  # only the callback reference remains
    latch(root, ident)
    thread = threading.Thread(target=callback)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outputs == [b'finished\n']
    assert f.busy_leases(root) == 0
    assert turns.acquire(root) is None


def test_cron_tick_refuses_late_and_retains_extension(node, monkeypatch):
    import hermes_constants
    from cron import scheduler, scheduler_tick
    root, ident = node
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda **kw: root)
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: root)
    with scheduler_tick.update_admission() as admitted:
        assert admitted
        latch(root, ident)
        extension = admission.extend(root)
        assert f.busy_leases(root) == 2
    assert f.busy_leases(root) == 1
    extension.close()
    with scheduler_tick.update_admission() as admitted:
        assert not admitted
    assert f.busy_leases(root) == 0


def test_backend_refuses_without_update_failopen(node):
    from hermes_cli.backend_retirement import _reserve
    root, ident = node
    latch(root, ident)
    reservation, reason = _reserve(root, 0, 'fixture')
    assert reservation is None
    assert reason == 'draining'
