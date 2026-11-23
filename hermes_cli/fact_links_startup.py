"""Startup-owned in-process links display projection. [daveey]

Only the launch origin is authorized here; other profile/storage contexts keep
legacy behavior. No replica IPC, request-side source discovery or strict-reader
cutover. The external origin inventory is recaptured at most every 30 seconds.
"""
from contextlib import asynccontextmanager
import asyncio
import os
import threading

from hermes_cli.fact_content_owner import ContentOwner
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_remote_links import LinksBudget, LinksFacts, RemoteLinksSource
from hermes_cli.fact_process import store as process_store

_lock = threading.RLock()
_runtimes = {}


def binding_for(origin):
    """Memory-only exact-origin selection; snapshot publication owns its lock."""
    with _lock:
        runtime = _runtimes.get(origin)
        return runtime.binding if runtime is not None else None


class LinksRuntime:
    def __init__(self, origin):
        self.origin = origin
        self.scope = Scope(origin[0], origin[1])
        self.store = process_store()
        self.owner = None
        self.binding = None
        self.closed = False
        self.v1 = self  # LinksFacts' scoped snapshot interface, no subscriber workers needed.

    def snapshot(self, scope):
        if self.closed or self.owner is None or not self.owner.feed._thread.is_alive():
            raise RuntimeError('links owner unavailable')
        if scope != self.scope:
            raise ValueError('unauthorized links scope')
        return self.store.snapshot(scope)

    def start(self, *, timeout=5):
        from hermes_cli import kanban_session_activity as activity
        with _lock:
            if self.origin in _runtimes:
                raise RuntimeError('links startup custody already retained')
            _runtimes[self.origin] = self
        # Never hold the publication lock while joining a legacy projection.
        activity._links_refresher.drain(self.origin, timeout=timeout)
        engine = ReducerEngine(self.store, self.scope)
        source = RemoteLinksSource(engine, SourceId(self.scope, 'remote.links', 'origin'), self.origin)
        try:
            self.owner = ContentOwner({self.scope: engine}, [], operational=[source],
                                      budget=LinksBudget())
        except BaseException as exc:
            # ContentStartupError carries an owner whose failed close must be retried.
            self.owner = getattr(exc, 'owner', None)
            raise
        with _lock:
            self.binding = LinksFacts(self, self.scope, self.origin)
        return self

    def close(self):
        from hermes_cli import kanban_session_activity as activity
        if self.owner is not None:
            self.owner.close()  # failure keeps both custody and the admission fence
        with _lock:
            self.closed = True
            if _runtimes.get(self.origin) is self:
                del _runtimes[self.origin]
            self.binding = None
        activity._links_refresher.resume(self.origin)


@asynccontextmanager
async def lifespan(app):
    """Select before other serve services, and unwind after their shutdown."""
    from hermes_cli import kanban_session_activity as activity
    origin = activity._origin_scope()
    # health: allow HX002 -- task-defined startup-only emergency rollback switch; never stored as a credential
    if os.environ.get('HERMES_FACTS_LINKS', '1').strip().lower() in ('0', 'off', 'false', 'no'):
        with _lock:
            retained = _runtimes.get(origin)
        if retained is not None:
            await asyncio.to_thread(retained.close)
        else:
            activity._links_refresher.resume(origin)
        yield
        return
    runtime = LinksRuntime(origin)
    # Start on the lifespan task so the adapter captures the launch context.
    # Drain may wait briefly, but no request is admitted until it completes.
    try:
        runtime.start()
        yield
    finally:
        # A duplicate app must not release another app's scope fence.
        with _lock:
            owned = _runtimes.get(origin) is runtime
        if owned:
            await asyncio.to_thread(runtime.close)
