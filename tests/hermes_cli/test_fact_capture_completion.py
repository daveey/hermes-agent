"""Capture result availability must not precede the completion wake's custody."""
import threading

from hermes_cli.fact_capture import CaptureBudget
from tests.hermes_cli import test_fact_board_inputs as helpers
from tests.waiting import wait_until

source = helpers.source


def test_completion_wakes_only_after_context_exit_and_job_retirement(source):
    adapter, _engine, calls, _woke = source
    context = adapter._context
    published, release, completed = threading.Event(), threading.Event(), threading.Event()
    notifications = []

    class HeldContext:
        def run(self, *args):
            result = context.run(*args)
            published.set()
            assert release.wait(5), 'test did not release capture context'
            return result

    def wake():
        notifications.append(adapter._worker.is_alive())
        completed.set()

    adapter.want('board', {'n': 1})
    adapter.want('board', {'n': 2})
    adapter._context = HeldContext()
    adapter._wake = wake
    try:
        adapter.capture(None, CaptureBudget(max_seconds=2))
        assert published.wait(2)
        # The result may be consumed immediately, but pending work must receive
        # another wake only after it can enter the saved context again.
        result = adapter.capture(None, CaptureBudget(max_seconds=2))
        adapter.commit(result)
        adapter.capture(None, CaptureBudget(max_seconds=2))
        assert notifications == [], 'result woke feed before the capture job retired'
        release.set()
        assert completed.wait(2), 'completion did not wake the pending selector'
        assert notifications == [False]
        adapter._context = context
        adapter.capture(None, CaptureBudget(max_seconds=2))
        assert wait_until(lambda: len(calls) == 2, timeout=2)
        adapter._worker.join(2)
        assert [view for _thread, view, _selector in calls] == ['board', 'board']
        assert [selector['n'] for _thread, _view, selector in calls] == [1, 2]
    finally:
        release.set()
        if adapter._worker is not None:
            adapter._worker.join(2)


def test_completion_after_close_does_not_notify_withdrawn_owner(source):
    adapter, _engine, _calls, woke = source
    adapter.close()
    woke.clear()
    adapter._capture_completed()
    assert not woke.is_set()


def test_cancelled_capture_releases_completion_callback():
    from hermes_cli.fact_capture_queue import CaptureJob
    calls = []
    job = CaptureJob(lambda: calls.append('capture'), lambda: calls.append('complete'))
    job.join(0)
    assert not job.is_alive() and job.cancelled
    assert job.callback is None and job.on_complete is None
    assert calls == []
