"""Authorized native content adapters composed with the shared owner. [daveey]

No discovery and no activation: trusted startup supplies sources, their scalar
hydrators and the scope engines. Registrations are fixed before the thread
starts. This is the task/session content slice, not external/operational inputs
or a declaration that consumer TaskFact/SessionFact schemas are ready.
"""
from dataclasses import dataclass

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_process_owner import ProcessOwner
from hermes_cli.fact_demand_source import DemandSource
from hermes_cli.fact_content_rows import task_rows, session_rows
from hermes_cli.fact_session_source import SessionContentSource
from hermes_cli.fact_source_commit import SourcePublisher
from hermes_cli.fact_task_source import TaskContentSource


@dataclass(frozen=True)
class ContentBinding:
    """*clock_only*: publish only the source's SourceState (its cursor), never rows or membership. [daveey]

    For consumers that use a content source as an invalidation clock. On the hub (2026-11-23) the board and
    project-tree owners bound the main kanban.db (16,884 tasks, ~66 MiB as facts) and state.db (27,727 sessions,
    ~78 MiB) only for their SourceState, but every row was published into the shared 48 MiB process store: each
    publication failed fact_budget_exceeded and the feed rebuilt both sources every 5 s (~70% of a core).
    """
    source: object
    path: object
    hydrate: object = None
    clock_only: bool = False


def _ids_only(conn, ids):
    """Clock-only hydration: the cursor alone is published, so rows carry nothing but their id.
    Same input contract as the scalar row hydrators (capture transaction, <= 500 string ids)."""
    if not conn.in_transaction:
        raise ValueError('content hydration requires capture transaction')
    if len(ids) > 500 or any(type(identity) is not str for identity in ids):
        raise ValueError('invalid hydration id batch')
    return ({'id': identity} for identity in ids)


class ContentStartupError(RuntimeError):
    """Failed startup whose cleanup must be retried through ``owner.close()``."""
    def __init__(self, owner):
        super().__init__('content startup failed; cleanup custody retained')
        self.owner = owner


class ContentOwner:
    """One ChangeFeed thread across all authorized board/shadow/session inputs.

    Engines must be fully configured before construction and thereafter mutated
    only by this feed. Runtime demands use bounded mailboxes on that same thread;
    discovery and production projection registration remain startup obligations.
    Engines can share one FactStore; a source capture and its affected outputs
    are one atomic generation, not a cross-database snapshot guarantee.
    """
    def __init__(self, engines, bindings, *, budget=CaptureBudget(), operational=(), lazy_sources=False):
        self._publishers = {}
        self._by_source = {}
        for binding in bindings:
            source = binding.source
            if source in self._by_source:
                raise ValueError('duplicate source identity')
            if source.scope not in engines:
                raise ValueError('unauthorized source scope')
            if source.kind == 'tasks.content':
                adapter_type, kind, rows = TaskContentSource, 'TaskContentInput', task_rows
            elif source.kind == 'sessions.content':
                adapter_type, kind, rows = SessionContentSource, 'SessionContentInput', session_rows
            else:
                raise ValueError('unsupported content source kind')
            if binding.clock_only and binding.hydrate is not None:
                raise ValueError('clock-only binding takes no hydrate')
            hydrate = _ids_only if binding.clock_only else rows if binding.hydrate is None else binding.hydrate
            adapter = adapter_type(source, binding.path, hydrate)
            publisher = SourcePublisher(engines[source.scope], source, kind, clock_only=binding.clock_only)
            self._publishers[adapter] = publisher
            self._by_source[source] = publisher
        for adapter in operational:
            if engines.get(adapter.source.scope) is not adapter.engine:
                raise ValueError('unauthorized operational engine')
            if adapter.source in self._by_source:
                raise ValueError('duplicate source identity')
            if any(getattr(item, 'key', None) == adapter.key for item in self._publishers):
                raise ValueError('duplicate operational output key')
            self._publishers[adapter] = adapter
            self._by_source[adapter.source] = adapter
        if not self._publishers:
            raise ValueError('empty content source set')
        self._demands = {scope: DemandSource(engine) for scope, engine in engines.items()}
        self._publishers.update((source, source) for source in self._demands.values())
        self.feed = ProcessOwner(self._commit, self._failure, budget=budget,
                                 stores={scope: engine.store for scope, engine in engines.items()})
        self._activation = None
        try:
            if lazy_sources:
                from hermes_cli.fact_scope_activation import ScopeActivation
                self._activation = ScopeActivation(engines, self._activate)
                self._publishers[self._activation] = self._activation
                adapters = (*self._demands.values(), self._activation)
            else:
                adapters = tuple(self._publishers)
            for adapter in adapters:
                self.feed.register(adapter)
        except Exception:  # health: allow BLE001 -- construction unwind retains owner custody on failure
            try:
                self.feed.close()
            except Exception as cleanup_error:  # health: allow BLE001 -- attach exact owner for cleanup retry
                raise ContentStartupError(self) from cleanup_error
            raise

    def publisher(self, source):
        """Startup key/metadata seam, not a request-side source resolver."""
        return self._by_source[source]

    def _activate(self, scope):
        for adapter in tuple(self._publishers):
            if getattr(getattr(adapter, 'source', None), 'scope', None) == scope:
                self.feed.register(adapter)

    def _commit(self, adapter, capture):
        self._publishers[adapter].commit(capture)

    def _failure(self, adapter, error):
        self._publishers[adapter].failure()

    def demand(self, scope, view, selector):
        """Authorize and enqueue only; reduction never runs on the caller thread."""
        if scope not in self._demands:
            raise ValueError('unauthorized demand scope')
        if not self.feed._thread.is_alive():
            raise RuntimeError('content owner closed or unavailable')
        if self._activation is not None:
            self._activation.want(scope)
        self._demands[scope].demand(view, selector)

    def close(self):
        for source in self._demands.values():
            source.close()
        self.feed.close()
