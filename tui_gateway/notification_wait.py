"""Interruptible due-time waits for notification pollers. [daveey]

Queue puts already signal not_empty. Out-of-queue handoffs and stops increment a
revision under that same mutex; a waiter snapshots it before inspecting sources,
then checks it atomically with waiting so a signal in between cannot be lost.
"""
import threading


def generation(queue):
    with queue.mutex:
        return getattr(queue, "_notification_generation", 0)


def wake(queue):
    with queue.not_empty:
        queue._notification_generation = getattr(queue, "_notification_generation", 0) + 1
        queue.not_empty.notify_all()


class PollerStop(threading.Event):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def set(self):
        super().set()
        wake(self.queue)


def due_timeout(now, deadlines, session, stop):
    timeout = max(0.0, min(deadlines) - now)
    # Held work is active work: its turn may finish without a queue put. Keep the
    # existing idle-boundary latency; do not make every *empty* session pay it.
    if session.get("_notif_held") or not isinstance(stop, PollerStop):
        timeout = min(timeout, 0.5)
    return timeout
