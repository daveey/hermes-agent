"""Exercise the real loop: a CPU wake is not a poll or board-service request."""
from types import SimpleNamespace
from pathlib import Path
import pytest
from hermes_cli import kanban_sync_client as client
from hermes_cli import machine_metrics as mm
from hermes_cli import machine_cpu_metrics as cpu

@pytest.mark.parametrize('period,links_every,expected_service,expected_links', [
    (30.0, 60.0, [1, 0], [100.0]),
    (1.0, 0.5, [1, 1], [100.0, 101.0]),
    (30.0, 0.5, [1, 0], [100.0, 101.0]),
])
def test_real_loop_clamps_only_pending_and_preserves_poll_and_links(monkeypatch, period, links_every,
                                                                    expected_service, expected_links):
    now, waits, services, links, sent = [100.0], [], [], [], []
    ident = SimpleNamespace(gateway='test', url='https://example.invalid')
    monkeypatch.setattr(client, 'load_identity', lambda: ident)
    monkeypatch.setattr(client, '_schema_ahead_boards', lambda b: [])
    monkeypatch.setattr(client, 'board_paths', lambda: [('a', Path('a.db'))])
    monkeypatch.setattr(client, 'Hub', lambda i: SimpleNamespace(report_metrics=sent.append))
    monkeypatch.setattr(client, 'HubLeaseClient', lambda h: None)
    monkeypatch.setattr(client.sync, 'set_lease_client', lambda h: None)
    monkeypatch.setattr(client.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(client, 'deferred_repairs_due', lambda *a: [])
    monkeypatch.setattr(client, 'next_deferred_repair', lambda *a: None)
    monkeypatch.setattr(client, 'outbox_pending', lambda *a: False)
    monkeypatch.setattr(client, 'LINKS_EVERY_S', links_every)
    monkeypatch.setattr(client, 'reconcile_links', lambda *a, **kw: links.append(now[0]))
    monkeypatch.setattr(client, 'sync_all_once', lambda h, *, boards: services.append(len(boards)) or [])
    class NoThread:
        def __init__(self, **kw):
            pass
        def start(self):
            pass
        def is_alive(self):
            return True
    monkeypatch.setattr(client.threading, 'Thread', NoThread)
    original = mm.Reporter
    rep = original(sent.append, clock=lambda: now[0], collect_fn=lambda: {'cpus': 4},
                   cpu_limit_fn=lambda: (cpu.ON, 50.0),
                   cpu_begin_fn=lambda **kw: (now[0], (100, 50)),
                   cpu_finish_fn=lambda *a, **kw: dict(cpu_busy_pct=10, cpu_busy_window_ms=1000,
                                                      cpu_busy_sample_end_ms=1700000000000))
    monkeypatch.setattr(mm, 'Reporter', lambda send: rep)
    monkeypatch.setattr(mm, 'record_hub_cpu_sample', lambda s: None)
    def wait(wake, paths, remaining, keep_running):
        waits.append(remaining)
        now[0] += remaining
        return 'period'
    monkeypatch.setattr(client, 'wait_for_work', wait)
    state = SimpleNamespace(clear=lambda: None, record_down=lambda msg: None)
    client._run_forever(lambda: len(waits) < 2, period, state)
    assert waits == [1.0, period if period == 1 else period-1]
    assert services == expected_service
    assert links == expected_links  # early links work is possible and belongs inside analyzer X
    assert len(sent) == 1 and rep.pending_wake_in() is None
