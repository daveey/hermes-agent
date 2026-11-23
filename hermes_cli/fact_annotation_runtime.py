"""Authorized annotation startup composition; no discovery or request I/O. [daveey]

The caller supplies exact profile homes and scoped seed identities. Construction
owns no release decision. ``start`` certifies captured inputs and seeded output
health before draining legacy; failed readiness never installs a native lookup.
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
from hermes_cli.fact_store import FactStore, view_key


class AnnotationRuntime:
    """One shared feed and transport, partitioned by exact authorized profile."""
    def __init__(self, profiles, *, fallback_profile, gateway, stop_legacy, start_legacy,
                 budget=CaptureBudget(max_seconds=5)):
        if not profiles or fallback_profile not in profiles:
            raise ValueError('annotation profile roster is incomplete')
        self.store = FactStore()
        self.scopes = {name: Scope('serve.annotations.' + gateway, name) for name in profiles}
        self.engines = {scope: ReducerEngine(self.store, scope) for scope in self.scopes.values()}
        self.handles, self.bindings = {}, {}
        self.owner = self.local = self.registry = None
        self._lock = threading.RLock()
        self._stopped = False
        self._native_profiles = set()
        self._closed = False
        self._stop, self._start = stop_legacy, start_legacy
        self.life = IncrementalLifecycle({(scope, 'annotations') for scope in self.scopes.values()})
        sources = []
        try:
            for name, home in profiles.items():
                scope, home = self.scopes[name], Path(home).absolute()
                engine = self.engines[scope]
                engine.register(view_reducer(projections()))
                sources.extend(self._sources(engine, scope, home, gateway))
            self.owner = NativeAnnotationOwner(self.engines, [], operational=sources, budget=budget)
            self.local = InProcessFacts(self.store, set(self.scopes.values()), demand=self.owner.demand)
            for name, scope in self.scopes.items():
                handle = self.local.acquire(scope, lifecycle=self.life)
                self.handles[name] = handle
                self.bindings[name] = annotations.AnnotationFacts(handle, scope,
                    stop_legacy=lambda name=name: self._pause(name),
                    start_legacy=lambda name=name: self._resume(name), view_facts=True)
            self.registry = AnnotationRegistry(self.bindings, fallback_profile)
        except BaseException:
            self.close()
            raise

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
        """Seed canonical selectors, await real source+output readiness, then cut over."""
        # Discovery includes config-only profiles. Only actual seeded consumers
        # gate cutover; absent stores retain unavailable evidence and reconcile
        # in the background. Optional PR information never gates serve.
        keys = {scope: set() for scope in self.scopes.values()}
        for profile, rows in seeds.items():
            scope = self.scopes[profile]
            for row in rows:
                for family in annotations.FIELDS:
                    selector = selector_for(scope, row, display=family == 'kanban')
                    self.owner.demand(scope, 'annotations.' + family, selector)
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
        for scope in self.scopes.values():
            self.life.report_ready('annotations', scope)
            self.life.transition('annotations', scope, 'facts', 1)
        self.registry.install()
        return self

    def close(self):
        if self._closed:
            return
        if self.registry is not None:
            self.registry.close()
        for name, binding in self.bindings.items():
            scope = self.scopes[name]
            self.life.transition('annotations', scope, 'legacy', 2)
            binding.close()
        if self.owner is not None:
            self.owner.close()
        for handle in self.handles.values():
            handle.close()
        if self.local is not None:
            self.local.close()
        self._closed = True
