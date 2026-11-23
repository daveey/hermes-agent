"""Shared board capture serialization and queued cancellation custody. [daveey]"""
import threading
from hermes_cli.fact_capture_queue import submit


def test_one_worker_and_cancelled_scope_never_runs():
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def blocked():
        calls.append('first')
        entered.set()
        assert release.wait(5)
    first = submit(blocked)
    try:
        assert entered.wait(5)
        second = submit(lambda: calls.append('cancelled'))
        third = submit(lambda: (calls.append('third'), completed.set()))
        assert sum(t.name == 'fact-board-capture' for t in threading.enumerate()) == 1
        second.join(.01)
        assert not second.is_alive()
        assert second.callback is None
        assert first.is_alive()
        release.set()
        assert completed.wait(5)
        third.join(5)
        assert calls == ['first', 'third']
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive()
