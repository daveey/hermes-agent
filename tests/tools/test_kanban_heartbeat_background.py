"""Auto liveness I/O must not block the agent's first user message."""
import contextvars
import pytest
import threading


def test_auto_heartbeat_does_not_wait_and_keeps_run_context(monkeypatch):
    from tools import kanban_tools as kt
    from tools.kanban_heartbeat_background import heartbeat_in_background

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    scope = contextvars.ContextVar('heartbeat-test-scope', default=None)
    token = scope.set(('profile', 'task', 'run', 'owner'))
    calls = []
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    monkeypatch.setattr(kt, '_is_delegated_child_context', lambda: False)
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
    monkeypatch.setattr(kt, '_is_delegated_child_context', lambda: True)
    monkeypatch.setattr(kt, 'heartbeat_current_worker_from_env', lambda: (_ for _ in ()).throw(AssertionError('not owner')))
    assert heartbeat_in_background() is None
    monkeypatch.setattr(kt, '_is_delegated_child_context', lambda: False)
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


def test_background_cron_activity_retains_original_heartbeat_scope(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    from tools import kanban_tools as kt
    from tools.kanban_heartbeat_background import heartbeat_in_background
    calls = []
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0)
    monkeypatch.setattr(kt, 'heartbeat_current_worker_from_env', lambda: calls.append('heartbeat'))
    with non_dispatcher_owned_context():
        worker = heartbeat_in_background()
        assert worker is not None
        worker.join(5)
    assert calls == ['heartbeat']


def test_background_activity_reports_fenced_descendant_once(monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace
    from agent.activity_tracking import ActivityTrackingMixin
    from tools import kanban_tools as kt
    import tools.kanban_heartbeat_background as background
    from tests.tools.test_delegate_kanban_isolation import _make_running_kanban_task
    import logging
    kb, tid, _, _ = _make_running_kanban_task(monkeypatch, tmp_path)
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        before = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        monkeypatch.setenv('HERMES_DELEGATED_CHILD_CONTEXT', str(tmp_path / '.hermes'))
        monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0)
        monkeypatch.setattr(kt, '_auto_heartbeat_fence_warned', False)
        monkeypatch.setattr(kt, 'inject_new_comments_from_env', lambda agent: None)
        monkeypatch.setattr(kt, 'inject_soft_deadline_from_env', lambda agent: None)
        agent = SimpleNamespace(_persist_session_activity_if_due=lambda: None)
        with caplog.at_level(logging.WARNING, logger='tools.kanban_tools'):
            for _ in range(2):
                monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0)
                ActivityTrackingMixin._touch_activity(agent, 'starting')
                worker = background._worker
                assert worker is not None
                worker.join(5)
                assert not worker.is_alive()
        warnings = [r for r in caplog.records if 'HERMES_DELEGATED_CHILD_CONTEXT' in r.getMessage()]
        assert len(warnings) == 1
        after = kb.get_task(conn, tid)
        assert after.claim_expires == before.claim_expires
        assert after.last_heartbeat_at == before.last_heartbeat_at
        assert kb.list_events(conn, tid) == events
    finally:
        conn.close()


@pytest.mark.parametrize('failure', [RuntimeError, OSError, 'drain'])
def test_background_start_refusal_is_logged_without_unfenced_fallback(monkeypatch, caplog, failure):
    import logging
    from hermes_cli.worker_drain_fence import DrainRefused
    from tools import kanban_tools as kt
    import tools.kanban_heartbeat_background as background
    calls = []
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    monkeypatch.setattr(kt, '_is_delegated_child_context', lambda: False)
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0)
    monkeypatch.setattr(background, '_worker', None)
    monkeypatch.setattr(kt, 'heartbeat_current_worker_from_env', lambda: calls.append('write'))
    error = DrainRefused if failure == 'drain' else failure
    class RefusedThread:
        def start(self):
            raise error('cannot start')
    monkeypatch.setattr(background, 'spawn_context_thread', lambda *a, **kw: RefusedThread())
    with caplog.at_level(logging.WARNING, logger=background.__name__):
        with pytest.raises(error):
            background.heartbeat_in_background()
    assert calls == []
    assert background._worker is None
    assert any('automatic worker heartbeat could not start' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('phase', ['construct', 'start'])
@pytest.mark.parametrize('failure', [RuntimeError, OSError, 'drain'])
def test_start_refusal_consumes_rate_window_and_keeps_steering(monkeypatch, caplog, phase, failure):
    import logging
    from types import SimpleNamespace
    from hermes_cli.worker_drain_fence import DrainRefused
    from agent.activity_tracking import ActivityTrackingMixin
    from tools import kanban_tools as kt
    import tools.kanban_heartbeat_background as background

    clock = [1000.0]
    attempts, writes, injections = [], [], []
    error = DrainRefused if failure == 'drain' else failure
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    monkeypatch.setattr(kt, '_is_delegated_child_context', lambda: False)
    monkeypatch.setattr(kt, '_auto_heartbeat_last_attempt', 0)
    monkeypatch.setattr(background, '_worker', None)
    monkeypatch.setattr(background.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(kt, 'heartbeat_current_worker_from_env', lambda: writes.append('write'))
    monkeypatch.setattr(kt, 'inject_new_comments_from_env', lambda agent: injections.append('comments'))
    monkeypatch.setattr(kt, 'inject_soft_deadline_from_env', lambda agent: injections.append('deadline'))

    class RefusedThread:
        def start(self):
            raise error('cannot start')

    def spawn(*args, **kwargs):
        attempts.append(phase)
        if phase == 'construct':
            raise error('cannot construct')
        return RefusedThread()

    monkeypatch.setattr(background, 'spawn_context_thread', spawn)
    agent = SimpleNamespace(_persist_session_activity_if_due=lambda: None)
    with caplog.at_level(logging.WARNING, logger=background.__name__):
        for _ in range(200):
            ActivityTrackingMixin._touch_activity(agent, 'stream chunk')
        warnings = [r for r in caplog.records if 'automatic worker heartbeat could not start' in r.getMessage()]
        assert len(attempts) == 1
        assert len(warnings) == 1
        assert warnings[0].exc_info[0] is error
        assert injections == ['comments', 'deadline'] * 200
        assert writes == []
        assert background._worker is None
        clock[0] += kt._AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS - 0.001
        assert background.heartbeat_in_background() is None
        clock[0] += 0.001
        with pytest.raises(error):
            background.heartbeat_in_background()
        assert len(attempts) == 2
        assert writes == []


@pytest.mark.parametrize('failure', ['resource', 'drain'])
def test_activity_thread_start_failure_keeps_operator_injections(monkeypatch, failure):
    from hermes_cli.worker_drain_fence import DrainRefused
    from types import SimpleNamespace
    from agent.activity_tracking import ActivityTrackingMixin
    calls = []
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'task')
    def unavailable():
        raise DrainRefused('draining') if failure == 'drain' else RuntimeError('cannot start thread')
    monkeypatch.setattr('tools.kanban_heartbeat_background.heartbeat_in_background', unavailable)
    monkeypatch.setattr('tools.kanban_tools.inject_new_comments_from_env', lambda agent: calls.append('comments'))
    monkeypatch.setattr('tools.kanban_tools.inject_soft_deadline_from_env', lambda agent: calls.append('deadline'))
    agent = SimpleNamespace(_persist_session_activity_if_due=lambda: calls.append('activity'))
    ActivityTrackingMixin._touch_activity(agent, 'starting')
    assert calls == ['comments', 'deadline', 'activity']
