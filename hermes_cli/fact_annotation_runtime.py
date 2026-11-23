"""Authorized annotation startup composition; no discovery or request I/O. [daveey]

The caller supplies exact profile homes and scoped seed identities. Construction
owns no release decision. ``start`` certifies captured inputs and seeded output
health before draining legacy; failed readiness never installs a native lookup.

Capture is per profile and lazy. Construction records only inert descriptors
(scopes, engines, homes). Seeded profiles activate in ``start``; an unseeded
profile's installed binding is a memory-only placeholder that fails closed
(``AnnotationUnavailable``, never legacy I/O) and enqueues one coalesced
background activation through ``fact_process.submit``.
"""
from contextvars import copy_context
from pathlib import Path
import threading
import time

from hermes_cli import fact_annotation_capture as capture
from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_membership import AnnotationMembershipSource
from hermes_cli.fact_annotation_sources import NativeAnnotationOwner, NativeAnnotationSource
from hermes_cli.fact_annotation_startup import AnnotationRegistry
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import FactKey, Scope, SourceId
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import view_key

_RETRY_SECONDS = 1.0


def _process():
    from hermes_cli import fact_process
    return fact_process


class _LazyBinding:
    """Installed consumer binding; memory-only and unavailable until its profile is active."""
    def __init__(self, runtime, profile, scope):
        self.runtime, self.profile, self.scope = runtime, profile, scope
        self.real: annotations.AnnotationFacts | None = None

    @property
    def _mode(self):
        real = self.real
        return 'legacy' if real is None else real._mode

    def annotate(self, family, rows, legacy, **kwargs):
        real = self.real
        if real is None:
            self.runtime._request(self.profile, rows)
            raise annotations.AnnotationUnavailable('annotation profile activating')
        # Registration precedes certification, and rollback precedes owner
        # release. Neither interval grants this installed binding legacy I/O.
        # Share the callback lock so mode cannot change between check and read.
        with real._lock:
            if (self.profile in self.runtime._cleanup_pending or real._mode != 'facts'
                    or real._closed):
                self.runtime._request(self.profile)
                raise annotations.AnnotationUnavailable('annotation profile unavailable')
            try:
                return real.annotate(family, rows, legacy, **kwargs)
            except annotations.AnnotationUnavailable:
                self.runtime._warm(self.profile, rows)
                raise

    def close(self):
        real = self.real
        if real is not None:
            real.close()


class AnnotationRuntime:
    """One shared feed and transport, partitioned by exact authorized profile."""
    def __init__(self, profiles, *, fallback_profile, gateway, stop_legacy, start_legacy,
                 budget=CaptureBudget(max_seconds=5)):
        if not profiles or fallback_profile not in profiles:
            raise ValueError('annotation profile roster is incomplete')
        self._process = _process()
        self.store = self._process.store()
        self.scopes = {name: Scope('serve.annotations.' + gateway, name) for name in profiles}
        self.engines = {scope: ReducerEngine(self.store, scope) for scope in self.scopes.values()}
        self._homes = {name: Path(home).absolute() for name, home in profiles.items()}
        self._gateway, self._budget = gateway, budget
        self.handles, self.owners, self.bindings = {}, {}, {}
        self.local = self.registry = None
        self._lock = threading.RLock()
        self._activation = threading.RLock()
        self._retry_at = {}
        self._cleanup_pending = set()
        self._registered = set()
        self._generations = {name: 0 for name in profiles}
        self._started = self._stopped = False
        self._native_profiles = set()
        self._closed = self._closing = False
        self._stop, self._start = stop_legacy, start_legacy
        self.life = IncrementalLifecycle({(scope, 'annotations') for scope in self.scopes.values()})
        self._lives = {name: self.life for name in profiles}
        try:
            self.local = InProcessFacts(self.store, set(self.scopes.values()), demand=self._demand)
            self.bindings = {name: _LazyBinding(self, name, scope) for name, scope in self.scopes.items()}
            self.registry = AnnotationRegistry(self.bindings, fallback_profile)
        except BaseException:
            self.close()
            raise

    def _demand(self, scope, view, selector):
        owner = self.owners.get(scope.profile_id)
        if owner is None:
            raise RuntimeError('annotation profile not active')
        owner.demand(scope, view, selector)

    def _pause(self, profile):
        with self._lock:
            self._native_profiles.add(profile)
            if not self._stopped:
                self._stopped = True
                self._stop()

    def _resume(self, profile):
        with self._lock:
            self._native_profiles.discard(profile)
            if self._stopped and not self._native_profiles:
                self._start()
                self._stopped = False

    def _warm(self, profile, rows):
        owner = self.owners.get(profile)
        scope = self.scopes[profile]
        if owner is None:
            return
        for row in rows:
            for family in annotations.FIELDS:
                try:
                    owner.demand(scope, 'annotations.' + family,
                                 selector_for(scope, row, display=family == 'kanban'))
                except (ValueError, TypeError, KeyError, RuntimeError):
                    return

    def _request(self, profile, rows=()):
        """Request-path seam: memory only. Enqueue one coalesced activation."""
        binding = self.bindings.get(profile)
        if (self._closing or not self._started or binding is None
                or (binding.real is not None and profile not in self._cleanup_pending)
                or time.monotonic() < self._retry_at.get(profile, 0)):
            return
        try:
            rows = [dict(row) for row in rows]
            self._process.submit(('fact_annotation_runtime.activate', id(self), profile),
                                 lambda: self._activate(profile, rows))
        except RuntimeError:
            pass  # control queue bounded or closed: stay unavailable; the caller retries

    def _build(self, profile):
        """Register capture sources, acquire the handle, bind. Caller holds ``_activation``."""
        if profile in self._cleanup_pending:
            self._teardown(profile)
        if profile in self.handles:
            return
        scope = self.scopes[profile]
        engine = self.engines[scope]
        if profile not in self._registered:
            engine.register(view_reducer(projections()))
            self._registered.add(profile)
        sources = self._sources(engine, scope, self._homes[profile], self._gateway)
        owner = handle = None
        try:
            owner = NativeAnnotationOwner({scope: engine}, [], operational=sources, budget=self._budget)
            self.owners[profile] = owner
            assert self.local is not None
            handle = self.local.acquire(scope, lifecycle=self._lives[profile])
            self.handles[profile] = handle
            self.bindings[profile].real = annotations.AnnotationFacts(handle, scope,
                stop_legacy=lambda: self._pause(profile),
                start_legacy=lambda: self._resume(profile), view_facts=True)
        except BaseException:
            self._teardown(profile, owner, handle)
            raise

    def _teardown(self, profile, owner=None, handle=None):
        self._cleanup_pending.add(profile)
        binding = self.bindings.get(profile)
        if binding is not None and binding.real is not None:
            self._generations[profile] += 1
            self._lives[profile].transition('annotations', self.scopes[profile], 'legacy',
                                           self._generations[profile])
            binding.close()
            binding.real = None
        owner = self.owners.get(profile, owner)
        if owner is not None:
            owner.close()
            self.owners.pop(profile, None)
        handle = self.handles.get(profile, handle)
        if handle is not None:
            handle.close()
            self.handles.pop(profile, None)
        # A released consumer capability is permanently retired. Only completed
        # cleanup permits a new participant; other profiles keep their authority.
        self._lives[profile] = IncrementalLifecycle({(self.scopes[profile], 'annotations')})
        self._cleanup_pending.discard(profile)

    def _certify(self, profile):
        scope = self.scopes[profile]
        self._lives[profile].report_ready('annotations', scope)
        self._generations[profile] += 1
        self._lives[profile].transition('annotations', scope, 'facts', self._generations[profile])

    def _activate(self, profile, rows=()):
        """Control-worker activation of an unseeded profile; failure stays unavailable."""
        with self._activation:
            if self._closing:
                return
            try:
                if profile in self._cleanup_pending:
                    self._teardown(profile)
                if self.bindings[profile].real is not None:
                    return
                self._build(profile)
                self._certify(profile)
                self._warm(profile, rows)
            except Exception:  # health: allow BLE001 -- failed activation retries later, never serves
                self._retry_at[profile] = time.monotonic() + _RETRY_SECONDS
                if profile not in self._cleanup_pending:
                    self._teardown(profile)
                raise

    @staticmethod
    def _sources(engine, scope, home, gateway):
        from agent.runtime_scope import bind_profile_scope
        from hermes_cli.fact_annotation_kanban import capture as kanban
        with bind_profile_scope(home, hydrate_secrets=False):
            context = copy_context()
        state = home / 'state.db'
        sources = [AnnotationMembershipSource(engine, SourceId(scope, 'annotations.' + family, 'local'), path)
                   for family, path in (('goal', state), ('cron', home / 'cron/jobs.json'))]
        readers = {
            'kanban': lambda budget, wall, ids: kanban(budget, now=wall),
            'background': lambda budget, wall, ids: capture.processes(home / 'processes.json', budget,
                profile=scope.profile_id, gateway=gateway, now=wall),
            'turn': lambda budget, wall, ids: capture.turns(state, budget,
                profile=scope.profile_id, gateway=gateway, now=wall),
            'subagent': lambda budget, wall, ids: capture.children(state, budget,
                profile=scope.profile_id, gateway=gateway, now=wall),
            'review': lambda budget, wall, ids: capture.reviews(state, budget, ids=ids, now=wall),
        }
        paths = (state, home / 'processes.json', home / 'desktop/interrupted_turns.json',
                 home / 'config.yaml', home / 'kanban')
        for family, reader in readers.items():
            def read(budget, wall, ids, reader=reader):
                return context.run(reader, budget, wall, ids)
            sources.append(NativeAnnotationSource(engine, SourceId(scope, 'annotations.' + family, 'local'),
                                                  paths, read))
        return sources

    def start(self, seeds, *, timeout=15):
        """Activate seeded profiles, await real source+output readiness, then cut over.

        Profiles without seeds register no capture source and acquire no handle;
        they activate later on demand through the installed lazy binding.
        """
        # Discovery includes config-only profiles. Only actual seeded consumers
        # gate cutover; absent stores retain unavailable evidence and reconcile
        # in the background. Optional PR information never gates serve.
        keys = {scope: set() for scope in self.scopes.values()}
        active = [profile for profile, rows in seeds.items() if rows]
        for profile in seeds:
            self.scopes[profile]  # unauthorized profile fails before any activation
        with self._activation:
            if self._closing:
                raise RuntimeError('annotation runtime closed')
            for profile in active:
                self._build(profile)
        for profile in active:
            scope = self.scopes[profile]
            for row in seeds[profile]:
                for family in annotations.FIELDS:
                    selector = selector_for(scope, row, display=family == 'kanban')
                    self.owners[profile].demand(scope, 'annotations.' + family, selector)
                    if family != 'review':
                        keys[scope].add(FactKey(scope, 'AnnotationInput', family))
                        keys[scope].add(view_key(scope, 'annotations.' + family, selector))
        deadline = time.monotonic() + timeout
        with self.store._condition:
            while True:
                now = time.time()
                if all((fact := self.store.snapshot(scope).get(key)) is not None and fact.health == 'fresh'
                       and (fact.valid_until is None or fact.valid_until > now)
                       for scope, wanted in keys.items() for key in wanted):
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError('native annotation sources/seeds not ready; legacy retained')
                self.store._condition.wait(left)
        with self._activation:
            for profile in active:
                self._certify(profile)
            self.registry.install()
            self._started = True
        return self

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closing = True
        with self._activation:
            if self.registry is not None:
                self.registry.close()
            for name in list(self.bindings):
                self._teardown(name)
            if self.local is not None:
                self.local.close()
            self._closed = True
