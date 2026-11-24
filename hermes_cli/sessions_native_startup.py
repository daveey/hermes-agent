"""App-owned sessions display startup; HERMES_FACTS_SESSIONS=0 rolls back. [daveey]

No configuration, discovery, capture or SQLite on the warm HTTP path. This is
in-process rendered-field composition, not dedicated fact_host activation.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context
import inspect
import logging
import os
import sys
import threading
from types import SimpleNamespace

from fastapi import HTTPException

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_process import FeedLease as ChangeFeed, store as process_store
from hermes_cli.fact_inprocess import IncrementalLifecycle, InProcessFacts
from hermes_cli.sessions_fact_routes import PENDING_NATIVE, use
from hermes_cli.sessions_native_source import SessionsSource


class SessionsRuntime:
    def __init__(self, profiles, targets, *, capture_reader=None):
        from hermes_cli.sessions_capture_process import CaptureProcess
        self.capture_reader = capture_reader if capture_reader is not None else CaptureProcess()
        self.profiles = profiles
        self.targets = tuple(targets)
        self.names = frozenset(name for name, _ in targets)
        self.scope = Scope(str(targets[0][1]), 'sessions-display')
        self.store = process_store()
        self.lifecycle = IncrementalLifecycle({(self.scope, 'sessions')})
        self.feed = self.local = self.handle = self.binding = None
        paths = [home / filename for _, home in targets for filename in ('state.db', 'config.yaml')]
        self.source = SessionsSource(self.store, SourceId(self.scope, 'sessions.display', 'canonical'),
                                     paths, self.capture, reconcile=self.reconcile,
                                     capture_many=self.capture_many if hasattr(self.capture_reader, 'capture_many') else None)
        self.generation = 0
        self.ready = False
        self._stopping = False
        self._bootstrap = None

    def start_background(self):
        context = copy_context()

        def bootstrap():
            try:
                self.start()
            except Exception:  # health: allow BLE001 -- isolate surface startup; retain owner for shutdown drain
                logging.getLogger(__name__).exception('sessions bootstrap unavailable; custody retained')

        # health: allow HX012 -- explicit copy_context keeps profile scope without importing the agent facade
        self._bootstrap = threading.Thread(target=lambda: context.run(bootstrap),
                                           name='sessions-bootstrap', daemon=True)
        self._bootstrap.start()

    def reconcile(self):
        # Profiles introduced after boot use 30s reconciliation until next boot
        # installs their native watches; do not claim native freshness for them.
        targets = self.profiles._profile_targets('sessions facts reconciliation')
        self.names = frozenset(name for name, _ in targets)

    def capture(self, name, selector):
        body = self.capture_reader(name, selector)
        self._observe(body)
        return body

    def capture_many(self, views):
        bodies = self.capture_reader.capture_many(views)
        for body in bodies:
            if isinstance(body, bytes):
                self._observe(body)
        return bodies

    def _observe(self, body):
        import json
        result = json.loads(body)
        groups = [result.get('sessions', ())]
        groups.extend(result.get(key, {}).get('sessions', ()) for key in ('recents', 'cron', 'messaging'))
        for rows in groups:
            for row in rows:
                if row.get('is_active'):
                    self.source.refresh_at(row.get('last_active', row.get('started_at', 0)) + 300)

    def wait_projection(self, name, selector, timeout):
        """Wait on owner memory only; capture remains on the feed thread."""
        from hermes_cli.fact_contract import Pending
        with self.store._condition:
            self.store._condition.wait_for(lambda: self._stopping or not isinstance(
                self.store.snapshot(self.scope).view(name, selector), Pending), timeout=timeout)

    def demand(self, scope, name, selector):
        if scope != self.scope or name not in ('sessions', 'sessions_sidebar', 'sessions_stamps'):
            raise ValueError('unauthorized sessions demand')
        profile = selector.get('recents_profile', selector.get('profile', 'all')) or 'all'
        if profile != 'all' and profile not in self.names:
            raise HTTPException(404, 'Profile not found')
        self.source.demand(name, selector)

    def start(self):
        self.feed = ChangeFeed(lambda source, capture: source.commit(capture),
                               lambda source, error: source.failure(),
                               budget=CaptureBudget(max_bytes=4 * 1024 * 1024, max_seconds=5),
                               stores={self.scope: self.store})
        try:
            from hermes_cli.sessions_fact_routes import selector_for
            selector = selector_for(inspect.signature(self.profiles.get_profiles_sessions), (), {})
            # Seed before registration so the first feed turn shares one child.
            self.demand(self.scope, 'sessions', selector)
            if hasattr(self.capture_reader, 'capture_many'):
                self.demand(self.scope, 'sessions', {**selector, 'limit': 50, 'min_messages': 1})
                sidebar = selector_for(inspect.signature(self.profiles.get_profiles_sessions_sidebar), (), {})
                self.demand(self.scope, 'sessions_sidebar', sidebar)
            self.feed.register(self.source)
            self.local = InProcessFacts(self.store, [self.scope], demand=self.demand)
            self.handle = self.local.acquire(self.scope, lifecycle=self.lifecycle)
            binding_handle = SimpleNamespace(v1=self.handle.v1, demand=self.handle.demand,
                                             touch=self.source.touch, wait_projection=self.wait_projection)
            self.binding = self.profiles.bind_sessions_facts(binding_handle, self.scope)
            from hermes_cli.fact_contract import Ready
            with self.store._condition:
                # Readiness gates this surface, not the server socket. An over-
                # budget cold capture remains unavailable and retries normally.
                self.store._condition.wait_for(lambda: self._stopping or isinstance(
                    self.store.snapshot(self.scope).view('sessions', selector), Ready))
                if self._stopping:
                    return self
                self.lifecycle.report_ready('sessions', self.scope)
                self.generation += 1
                self.lifecycle.transition('sessions', self.scope, 'facts', self.generation)
                self.ready = True
        except BaseException:
            if self._bootstrap is None:
                self.close()
            raise
        return self

    def close(self):
        with self.store._condition:
            self.ready = False
            self._stopping = True
            self.store._condition.notify_all()
        close_reader = getattr(self.capture_reader, 'close', None)
        if close_reader is not None:
            close_reader()
        if self._bootstrap is not None:
            self._bootstrap.join(5)
            if self._bootstrap.is_alive():
                raise TimeoutError('sessions bootstrap did not stop; custody retained')
        if self.binding is not None:
            self.generation += 1
            self.lifecycle.transition('sessions', self.scope, 'legacy', self.generation)
        if self.feed is not None:
            self.feed.close()  # failed drain retains app custody; never start a replacement
        if self.binding is not None:
            self.binding.close()
            self.binding = None
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        if self.local is not None:
            self.local.close()
            self.local = None


class SessionsMiddleware:
    """Use only app-startup authority, never infer a binding from a query value."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        runtime = getattr(scope['app'].state, 'sessions_facts', None)
        binding = None if runtime is None else (runtime.binding if runtime.ready else PENDING_NATIVE)
        with use(binding):
            await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app):
    from hermes_cli.web_routers import profiles
    retained = getattr(app.state, 'sessions_facts', None)
    if retained is not None:
        raise RuntimeError('sessions startup custody retained')
    # health: allow HX002 -- creator-defined startup rollback switch, never a credential
    enabled = os.environ.get('HERMES_FACTS_SESSIONS', '1').strip().lower() not in ('0', 'off', 'false', 'no')
    # NativeSourceWatch currently supports Linux only; keep other hosts legacy.
    if not enabled or not sys.platform.startswith('linux'):
        yield
        return
    targets = await asyncio.to_thread(profiles._profile_targets, 'sessions facts startup')
    runtime = SessionsRuntime(profiles, targets)
    app.state.sessions_facts = runtime  # custody survives a failed startup/unwind
    try:
        runtime.start_background()
        yield
    finally:
        await asyncio.to_thread(runtime.close)
        app.state.sessions_facts = None
