"""Legacy cron fail-open cannot leave an activated daemon exempt from /proc."""
import pytest

from cron import scheduler_tick as tick
from hermes_cli import admission_fence
from hermes_cli import worker_drain_capability as cap


@pytest.mark.parametrize('unavailable', ['unsupported', 'reserve_oserror'])
def test_failopen_revokes_exemption_before_dispatch(monkeypatch, unavailable):
    from cron import scheduler
    from hermes_cli import backend_retirement
    events = []
    monkeypatch.setattr(cap, 'revoke', lambda reason: events.append('revoked'))
    monkeypatch.setattr(admission_fence, 'SUPPORTED', unavailable != 'unsupported')
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: '/fixture/home')
    monkeypatch.setattr(backend_retirement, 'BUSY_WAIT_S', 0.01)

    def reserve(*a, **kw):
        raise OSError('fixture fence unavailable')

    monkeypatch.setattr(admission_fence, 'reserve', reserve)
    with tick.update_admission() as allowed:
        assert allowed
        events.append('dispatch')
    assert events == ['revoked', 'dispatch']


def test_import_failure_revokes_before_legacy_failopen(monkeypatch):
    import builtins

    events = []
    real_import = builtins.__import__

    def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'hermes_cli.admission_fence':
            raise ImportError('controlled unavailable admission')
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(cap, 'revoke', lambda reason: events.append('revoked'))
    monkeypatch.setattr(builtins, '__import__', unavailable)
    with tick.update_admission() as allowed:
        assert allowed
        events.append('dispatch')
    assert events == ['revoked', 'dispatch']

