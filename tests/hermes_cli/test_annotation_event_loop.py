"""Real native waits with independent lease maintenance."""
import resource
import threading
import time
from types import SimpleNamespace

from hermes_cli.annotation_event_loop import run
from hermes_cli.annotation_file_events import FileEvents


def test_idle_beats_do_not_read_inputs_and_change_wakes(tmp_path):
    source = tmp_path / 'source'
    source.write_text('one')
    stop = threading.Event()
    ready = threading.Event()
    updated = threading.Event()
    reads, beats = [], []
    with FileEvents() as events:
        def refresh():
            events.replace([source])
            reads.append(source.read_text() if source.exists() else None)
            ready.set()
            if len(reads) > 1:
                updated.set()
        lease = SimpleNamespace(held=lambda: True, beat=lambda: beats.append(1))
        usage = []
        def worker():
            before = resource.getrusage(resource.RUSAGE_THREAD)
            started = time.monotonic()
            run(events, stop, lease, refresh, interval=.2)
            after = resource.getrusage(resource.RUSAGE_THREAD)
            usage.append((after.ru_nvcsw - before.ru_nvcsw) / (time.monotonic() - started))
        thread = threading.Thread(target=worker, name='kanban-annotation-holder')
        thread.start()
        try:
            assert ready.wait(1)
            assert not updated.wait(61)
            assert len(beats) >= 3
            started = time.monotonic()
            source.unlink()
            assert updated.wait(1)
            assert time.monotonic() - started < 1
            assert reads == ['one', None]
        finally:
            stop.set()
            events.wake()
            thread.join(2)
        assert not thread.is_alive()
        assert usage[0] < 500
        print(f'isolated native scheduler voluntary cs/s: {usage[0]:.3f}')


def test_lease_errors_recover_without_reading_under_lost_ownership():
    stop = threading.Event()
    calls = []
    held_calls = []
    beat_calls = []

    class Events:
        def wait(self, timeout):
            stop.wait(timeout)
            return False

    def held():
        held_calls.append(1)
        if len(held_calls) == 1:
            raise OSError('lease temporarily unreadable')
        return True

    def beat():
        beat_calls.append(1)
        if len(beat_calls) == 1:
            raise OSError('lease heartbeat failed')

    def refresh():
        calls.append((len(held_calls), len(beat_calls)))
        stop.set()

    # Bounded even if recovery regresses; exceptions remain test failures.
    timer = threading.Timer(1, stop.set)
    timer.start()
    try:
        run(Events(), stop, SimpleNamespace(held=held, beat=beat), refresh, interval=.01)
    finally:
        timer.cancel()
        timer.join()
    assert calls == [(3, 2)]


def test_one_shot_deadline_and_read_error_retry(tmp_path):
    stop = threading.Event()
    done = threading.Event()
    calls = []
    with FileEvents() as events:
        def refresh():
            events.replace([])
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise OSError('transient input failure')
            if len(calls) == 2:
                return time.time() + .05
            done.set()
            return None
        lease = SimpleNamespace(held=lambda: True, beat=lambda: None)
        thread = threading.Thread(target=run, args=(events, stop, lease, refresh), kwargs={'interval': .02})
        thread.start()
        try:
            assert done.wait(1)
            assert not stop.wait(.08)
            assert len(calls) == 3
        finally:
            stop.set()
            events.wake()
            thread.join(2)
        assert not thread.is_alive()
