"""Scoped local readers and callback-drain custody. [daveey]"""
from hermes_cli.fact_runtime import ScopedHandle


class LocalHandle(ScopedHandle):
    def close(self):
        self._replica.owner.check_control_thread()
        super().close()


class LocalSubscription:
    def __init__(self, reader, subscription):
        self.reader, self.subscription = reader, subscription

    def close(self):
        reader, subscription = self.reader, self.subscription
        subscription.close()
        # Consumer transitions often hold a lock also used by their callback.
        # Cancellation cannot join here: the outer handle close owns the fence.
        with reader.owner.store._condition:
            if subscription.delivery_lock.locked():
                reader.retiring.add(subscription)
            reader.subscriptions.discard(self)
            reader.owner.store._condition.notify_all()


class LocalReader:
    def __init__(self, owner, scope):
        self.owner, self.scope = owner, scope
        self.v1 = self
        self.subscriptions = set()
        self.retiring = set()
        self.closed = False

    def snapshot(self, scope):
        self.owner.check_running()
        return self.owner.store.snapshot(scope)

    def changes(self, scope, after, *, kinds=(), keys=None):
        self.owner.check_running()
        return self.owner.store.changes(scope, after, kinds=kinds, keys=keys)

    def subscribe(self, scope, kinds, keys, after, callback):
        with self.owner.store._condition:
            self.owner.check_running()
            if self.closed:
                raise RuntimeError('local reader closed')
            raw = self.owner.store.subscribe(scope, kinds, keys, after, callback)
            subscription = LocalSubscription(self, raw)
            self.subscriptions.add(subscription)
            self.owner.store._condition.notify_all()
            return subscription

    def demand(self, view, selector):
        self.owner.check_running()
        # Startup callback must enqueue work, not run reducers on this thread.
        self.owner.demand(self.scope, view, selector)

    def close(self):
        self.owner.check_control_thread()
        with self.owner.store._condition:
            self.closed = True
            subscriptions = tuple(self.subscriptions)
        for subscription in subscriptions:
            subscription.close()
        with self.owner.store._condition:
            retiring = tuple(self.retiring)
        for subscription in retiring:
            if not subscription.delivery_lock.acquire(timeout=5):
                raise TimeoutError('local callback still running; custody retained')
            subscription.delivery_lock.release()
        with self.owner.store._condition:
            self.retiring.clear()
            self.owner._readers.discard(self)
            self.owner.store._condition.notify_all()
