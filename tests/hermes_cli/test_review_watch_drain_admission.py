"""Native admission remains live after a timed-out last-watch removal."""
import os
import threading

import pytest
from hermes_cli import fact_source_watch as native


@pytest.mark.platforms('linux')
def test_new_watch_after_timed_out_last_remove_stays_live(tmp_path, monkeypatch):
    a = tmp_path / 'a.db'
    b = tmp_path / 'b.db'
    a.write_text('initial')
    entered, release, admitted = threading.Event(), threading.Event(), threading.Event()
    hint = threading.Event()
    watch = native.NativeSourceWatch((a,), lambda: None)
    owner = watch._owner
    original = native.FileEvents.replace
    real_wait = native.FileEvents.wait
    armed = True

    def wait_loss(self, timeout=None):
        changed = real_wait(self, timeout)
        if changed and self is getattr(watch, '_events', None):
            self.coverage_lost = True
        return changed

    def delayed_rearm(events, *args, **kwargs):
        nonlocal armed
        if events is watch._events and armed:
            armed = False
            entered.set()
            assert release.wait(15)
        return original(events, *args, **kwargs)

    monkeypatch.setattr(native.FileEvents, 'wait', wait_loss)
    monkeypatch.setattr(native.FileEvents, 'replace', delayed_rearm)
    result = {}
    successor = None
    thread = None
    try:
        a.write_text('changed')
        assert entered.wait(5)
        with pytest.raises(TimeoutError):
            watch.close()  # actual five-second timeout; removal stays queued
        submit = owner.submit

        def mark_admission(action):
            request = submit(action)
            admitted.set()
            return request

        monkeypatch.setattr(owner, 'submit', mark_admission)

        def create():
            try:
                result['watch'] = native.NativeSourceWatch((b,), hint.set)
            except BaseException as exc:
                result['error'] = exc

        thread = threading.Thread(target=create)
        thread.start()
        assert admitted.wait(3)
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert 'error' not in result, result.get('error')
        successor = result['watch']
        owner._thread.join(2)
        successor.check()
        assert successor._owner._thread.is_alive(), 'successful watch admitted onto a dead owner'
        b.write_text('new source')
        assert hint.wait(5)
    finally:
        release.set()
        if thread is not None:
            thread.join(5)
        watch.close()
        if successor is not None:
            descriptor = successor._events._fd
            successor.close()
            if successor in owner._watches:
                # Keep the authentic broken-owner RED fixture leak-free.
                successor._events.close()
                owner._watches.discard(successor)
            else:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
