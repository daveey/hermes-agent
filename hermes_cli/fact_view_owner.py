"""Scoped in-process canonical view ownership on ContentOwner. [daveey]

Trusted startup supplies source paths, identities and detached capture/render
functions. Demands only enqueue; no request discovery, source access or reducer
execution occurs here. This is an in-process transport, not a dedicated host.
"""
from hermes_cli.fact_board_inputs import BoardInputSource
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_content_owner import ContentOwner
from hermes_cli.fact_inprocess import IncrementalLifecycle, InProcessFacts
from hermes_cli.fact_materializers import Projection, view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_process_owner import process_store


class ViewOwner:
    def __init__(self, scope, capture, render, views, *, paths=(), bindings=(), consumer='board'):
        self.scope, self.consumer = scope, consumer
        self.store = process_store()
        engine = ReducerEngine(self.store, scope)
        from hermes_cli.fact_contract import SourceId
        self.source = BoardInputSource(engine, SourceId(scope, 'board.input', 'native'), capture, paths=paths)
        self.owner = self.transport = self.handle = None
        self.lifecycle = IncrementalLifecycle({(scope, consumer)})
        self._bindings = tuple(bindings)
        self._states = []

        def projection(view):
            def dependencies(scope, selector, snapshot):
                return {self.source.input_key(view, selector), *self._states}
            def reduce(selector, facts, now):
                return render(view, facts[self.source.input_key(view, selector)].value)
            return Projection(view, ('BoardInput', 'SourceState'), dependencies, reduce)
        engine.register(view_reducer(projection(view) for view in views))
        self.engine = engine

    def start(self):
        if self.owner is not None:
            raise RuntimeError('view owner already started')
        try:
            self.owner = ContentOwner({self.scope: self.engine}, self._bindings,
                                      operational=[self.source], budget=CaptureBudget(max_seconds=10))
        except BaseException as exc:
            self.owner = getattr(exc, 'owner', None)
            raise
        self._states.extend(self.owner.publisher(binding.source).state_key for binding in self._bindings)
        self.transport = InProcessFacts(self.store, [self.scope], demand=self.demand)
        self.handle = self.transport.acquire(self.scope, lifecycle=self.lifecycle)
        return self.handle

    def demand(self, scope, view, selector):
        if scope != self.scope:
            raise ValueError('unauthorized view scope')
        if not self.source.want(view, selector):
            return False
        self.owner.demand(scope, view, selector)
        return True

    def close(self):
        errors = []
        for resource in (self.handle, self.transport, self.owner):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:  # health: allow BLE001 -- drain every resource; retain all failed custody for retry
                    errors.append(exc)
        if errors:
            raise ExceptionGroup('view owner shutdown incomplete', errors)
