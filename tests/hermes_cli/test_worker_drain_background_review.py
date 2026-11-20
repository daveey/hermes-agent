"""Background review custody precedes spawn and outlives target plus requeue."""
from types import SimpleNamespace

import pytest

from hermes_cli import worker_drain_fence as fence


@pytest.mark.parametrize('start_fails', [False, True])
def test_review_retains_independent_lease_through_outermost_cleanup(monkeypatch, start_fails):
    from agent import background_review, local_work_agent, review_idle_queue
    from hermes_cli.backend_retirement import retirement
    import run_agent

    events, targets = [], []
    lease = SimpleNamespace(closed=False)

    def close():
        lease.closed = True
        events.append('close')

    lease.close = close
    monkeypatch.setattr(fence, 'admit', lambda purpose: (events.append('admit') or lease))
    monkeypatch.setattr(local_work_agent, 'reserve', lambda *a: None)
    monkeypatch.setattr(background_review, 'prepare_background_review_run', lambda agent: SimpleNamespace())
    monkeypatch.setattr(background_review, 'finish_background_review_run', lambda *a: None)
    monkeypatch.setattr(retirement, 'begin_background_review', lambda *a: object())
    monkeypatch.setattr(retirement, 'end_background_review', lambda *a: events.append('retirement-end'))

    def target():
        assert not lease.closed
        assert fence._authority.get() is lease
        events.append('target')

    monkeypatch.setattr(background_review, 'spawn_background_review_thread', lambda *a, **k: (target, 'prompt'))

    def requeue(*a):
        assert not lease.closed
        events.append('requeue')
        return True

    monkeypatch.setattr(review_idle_queue, 'requeue_update_paused_review', requeue)

    class Thread:
        def __init__(self, *, target, **kwargs):
            targets.append(target)

        def start(self):
            assert events == ['admit']
            if start_fails:
                raise RuntimeError('spawn failed')

    monkeypatch.setattr(run_agent.threading, 'Thread', Thread)
    agent = SimpleNamespace(session_id='fixture', _REVIEW_REQUEUE_MAX_ATTEMPTS=3)
    if start_fails:
        with pytest.raises(RuntimeError, match='spawn failed'):
            run_agent.AIAgent._spawn_background_review_now(agent, [])
        assert lease.closed
    else:
        run_agent.AIAgent._spawn_background_review_now(agent, [])
        assert events == ['admit'] and not lease.closed
        targets[0]()
        assert events == ['admit', 'target', 'retirement-end', 'requeue', 'close']
        assert fence._authority.get() is None
