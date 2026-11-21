"""Auto liveness I/O must not block the agent's first user message."""
import contextvars
import threading


def test_auto_heartbeat_does_not_wait_and_keeps_run_context(monkeypatch):
    from tools import kanban_tools as kt
    from tools.kanban_heartbeat_background import heartbeat_in_background

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    scope = contextvars.ContextVar('heartbeat-test-scope', default=None)
    token = scope.set(('profile', 'task', 'run', 'owner'))
    calls = []
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    monkeypatch.setattr(kt, '_is_dispatcher_owned_worker', lambda: True)
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0)

    def heartbeat():
        try:
            calls.append(scope.get())
            entered.set()
            assert release.wait(5)
        finally:
            finished.set()

    monkeypatch.setattr(kt, 'heartbeat_current_worker_from_env', heartbeat)
    try:
        worker = heartbeat_in_background()
        assert entered.wait(5)
        assert not finished.is_set(), 'scheduling waited for the network call'
        assert heartbeat_in_background() is None
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert calls == [('profile', 'task', 'run', 'owner')]
    finally:
        release.set()
        if 'worker' in locals() and worker is not None:
            worker.join(5)
        scope.reset(token)


def test_delegates_and_rate_limited_activity_schedule_nothing(monkeypatch):
    import time
    from tools import kanban_tools as kt
    from tools.kanban_heartbeat_background import heartbeat_in_background
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    monkeypatch.setattr(kt, '_is_dispatcher_owned_worker', lambda: False)
    monkeypatch.setattr(kt, 'heartbeat_current_worker_from_env', lambda: (_ for _ in ()).throw(AssertionError('not owner')))
    assert heartbeat_in_background() is None
    monkeypatch.setattr(kt, '_is_dispatcher_owned_worker', lambda: True)
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', time.monotonic())
    assert heartbeat_in_background() is None
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0)
    assert heartbeat_in_background() is None


def test_activity_calls_background_bridge_and_keeps_operator_injection(monkeypatch):
    from types import SimpleNamespace
    from agent.activity_tracking import ActivityTrackingMixin
    calls = []
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    monkeypatch.setattr('tools.kanban_heartbeat_background.heartbeat_in_background', lambda: calls.append('background'))
    monkeypatch.setattr('tools.kanban_tools.heartbeat_current_worker_from_env',
                        lambda: (_ for _ in ()).throw(AssertionError('synchronous heartbeat')))
    monkeypatch.setattr('tools.kanban_tools.inject_new_comments_from_env', lambda agent: calls.append('comments'))
    monkeypatch.setattr('tools.kanban_tools.inject_soft_deadline_from_env', lambda agent: calls.append('deadline'))
    agent = SimpleNamespace(_persist_session_activity_if_due=lambda: calls.append('activity'))
    ActivityTrackingMixin._touch_activity(agent, 'starting')
    assert calls == ['background', 'comments', 'deadline', 'activity']
    assert agent._last_activity_desc == 'starting'
