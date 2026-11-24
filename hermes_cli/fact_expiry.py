"""Owner deadline publication, never request-time clock reads. [daveey]"""
import heapq
import threading
import time

from hermes_cli.fact_contract import evolve


class ExpiryIndex:
    def __init__(self):
        self.heap = []
        self.current = {}
        self.serial = 0

    def record(self, key, fact):
        self.current.pop(key, None)
        if fact is not None and fact.health == "fresh" and fact.valid_until is not None:
            self.serial += 1
            item = (fact.valid_until, self.serial, key, fact.rev)
            self.current[key] = item
            heapq.heappush(self.heap, item)
        if len(self.heap) > max(64, 2 * len(self.current)):
            self.heap = list(self.current.values())
            heapq.heapify(self.heap)

    def next(self):
        while self.heap and self.current.get(self.heap[0][2]) != self.heap[0]:
            heapq.heappop(self.heap)
        return self.heap[0][0] if self.heap else None

    def expire(self, store, now):
        prepared = []
        while self.next() is not None and self.next() <= now:
            _, _, key, rev = heapq.heappop(self.heap)
            self.current.pop(key, None)
            fact = store._facts.get(key)
            if fact is not None and fact.rev == rev:
                prepared.append((evolve(fact, health="stale", error_code="validity_expired", valid_until=None),
                                 store._sizes[key]))
        if prepared:
            store._publish(prepared, ())


class ExpiryWorker:
    def __init__(self, store):
        self.store = store
        self.stopped = False
        self.thread = threading.Thread(target=self.run, name="fact-expiry", daemon=True)
        self.thread.start()

    def run(self):
        with self.store._condition:
            while not self.stopped:
                self.store._expiry.expire(self.store, time.time())
                deadline = self.store._expiry.next()
                timeout = None if deadline is None else min(threading.TIMEOUT_MAX, max(0, deadline - time.time()))
                self.store._condition.wait(timeout)

    def close(self):
        with self.store._condition:
            self.stopped = True
            self.store._condition.notify_all()
        self.thread.join(5)
