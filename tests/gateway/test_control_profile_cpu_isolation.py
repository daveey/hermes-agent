"""Native CPU counters must distinguish sleeping and busy concurrent threads."""
import sys
import threading
import time

import pytest

from gateway.control_profile import _Window


@pytest.mark.platforms('linux', 'macos')
def test_sleeping_thread_does_not_inherit_burner_cpu():
    ready = threading.Barrier(3)
    start = threading.Event()
    burned = threading.Event()
    release = threading.Event()
    consumed = []

    def burner():
        ready.wait()
        start.wait()
        before = time.thread_time()
        while time.thread_time() - before < 0.35:
            pass
        consumed.append(time.thread_time() - before)
        burned.set()
        release.wait()

    def sleeper():
        ready.wait()
        release.wait()

    workers = [threading.Thread(target=burner, name='burner'),
               threading.Thread(target=sleeper, name='sleeper')]
    for worker in workers:
        worker.start()
    try:
        ready.wait(timeout=10)
        window = _Window()

        def tick(sample):
            before = {t.ident: t for t in threading.enumerate()}
            frames = sys._current_frames()
            after = {t.ident: t for t in threading.enumerate()}
            window.slow_tick(sample, True, False, before, frames, after)

        tick(0)  # Both native counters are baselined before the burner starts.
        start.set()
        assert burned.wait(30), 'burner did not finish 0.35 thread-CPU seconds'
        tick(1)
        window.flush()
        rows = {row['name']: row for row in window.finish(2, 10)}
        assert consumed[0] >= 0.3
        assert rows['sleeper']['cpu_s'] < 0.05
        assert rows['burner']['cpu_s'] >= 0.2
        assert not rows['sleeper']['cpu_partial']
        assert not rows['burner']['cpu_partial']
    finally:
        start.set()
        release.set()
        for worker in workers:
            worker.join(30)
