"""Notification queue waits are interruptible without a fixed idle timer. [daveey]"""
import queue
import threading


def test_stop_wakes_waiter_and_pre_wait_stop_is_not_lost():
    from tui_gateway.notification_wait import PollerStop, generation
    from tools.process_registry_awaiting import take_ready
    q = queue.Queue()
    stop = PollerStop(q)
    before = generation(q)
    stop.set()
    finished = threading.Event()
    thread = threading.Thread(target=lambda: (take_ready(q, 5, wake_generation=before), finished.set()))
    thread.start()
    try:
        assert finished.wait(0.5)
    finally:
        with q.not_empty:
            q.not_empty.notify_all()
        thread.join(1)


def test_queue_put_wakes_long_wait_and_keeps_custody():
    from tui_gateway.notification_wait import generation
    from tools.process_registry_awaiting import take_ready, release
    q = queue.Queue()
    before = generation(q)
    finished = threading.Event()
    got = []
    def consume():
        got.extend(take_ready(q, 5, wake_generation=before))
        finished.set()
    thread = threading.Thread(target=consume)
    thread.start()
    event = {"type": "completion", "owner_task_id": "idle-wait"}
    try:
        q.put(event)
        assert finished.wait(0.5)
        assert got == [event]
    finally:
        with q.not_empty:
            q.not_empty.notify_all()
        thread.join(1)
        release(got)


def test_held_handoff_wakes_all_waiters():
    from tui_gateway.notification_wait import generation
    from tools.process_registry_awaiting import take_ready
    from tui_gateway import server
    q = queue.Queue()
    before = generation(q)
    owner = {"_notif_held": []}
    assert server._notif_hold_for(owner, {"type": "completion"}, q)
    finished = threading.Event()
    thread = threading.Thread(target=lambda: (take_ready(q, 5, wake_generation=before), finished.set()))
    thread.start()
    try:
        assert finished.wait(0.5)
    finally:
        with q.not_empty:
            q.not_empty.notify_all()
        thread.join(1)


def test_wake_releases_multiple_actual_waiters():
    from tui_gateway.notification_wait import generation, wake
    from tools.process_registry_awaiting import take_ready
    q = queue.Queue()
    before = generation(q)
    barrier = threading.Barrier(4)
    finished = [threading.Event() for _ in range(3)]
    def consume(done):
        barrier.wait()
        take_ready(q, 5, wake_generation=before)
        done.set()
    threads = [threading.Thread(target=consume, args=(done,)) for done in finished]
    for thread in threads:
        thread.start()
    try:
        barrier.wait()
        wake(q)
        assert all(done.wait(0.5) for done in finished)
    finally:
        wake(q)
        for thread in threads:
            thread.join(1)


def test_held_work_keeps_busy_to_idle_latency_bounded():
    from tui_gateway.notification_wait import PollerStop, due_timeout
    stop = PollerStop(queue.Queue())
    session = {"running": True, "_notif_held": [{"type": "completion"}]}
    assert due_timeout(10, [15], session, stop) == 0.5
    session["running"] = False
    assert due_timeout(10, [15], session, stop) == 0.5
    session["_notif_held"] = []
    assert due_timeout(10, [15], session, stop) == 5


def test_both_park_producers_publish_a_wake(monkeypatch):
    from tui_gateway import notification_park
    from tui_gateway.notification_wait import generation
    from tools.process_registry import process_registry
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    monkeypatch.setattr(process_registry, "_parked_events", [])
    notification_park.clear()
    event = {"type": "completion", "session_key": "park-proof"}
    before = generation(q)
    try:
        process_registry.park_unowned_event(event)
        assert generation(q) > before
        assert process_registry.has_parked_events()
        assert process_registry.take_parked_events(lambda e: True) == [event]
        assert not process_registry.has_parked_events()
        before = generation(q)
        assert notification_park.park(event)
        assert generation(q) > before
        assert notification_park.has_parked()
        taken = notification_park.claim(lambda e: True)
        assert taken == [event]
        notification_park.unhold(taken)
        assert not notification_park.has_parked()
    finally:
        notification_park.clear()
