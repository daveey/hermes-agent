"""Explicit startup-authorized dashboard lifecycle injection. [daveey]

Only trusted mount callers use this module, after existing plugin trust checks.
No profile discovery, source capture, or facts-mode activation occurs here.
Hosted plugins must install this registry inside their own isolated ASGI app.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import threading


class PluginLifecycles:
    """App-local bindings keyed by exact API path, never a global plugin name.

    The startup owner supplies already-authorized ctx and consumer kwargs. A
    failed close retains custody, preventing a replacement from starting loops.
    Lifecycle hooks must unwind their own partial setup when they raise before
    returning a handle. A returned handle exposes synchronous close().
    """
    def __init__(self):
        self._bindings = {}
        self._handles = {}
        self._modules = {}
        self._lock = threading.RLock()
        self._closed = False

    def authorize(self, api_path, ctx, kwargs):
        key = Path(api_path).resolve()
        with self._lock:
            if self._closed or key in self._handles or key in self._bindings:
                raise RuntimeError("plugin binding already owned or closed")
            self._bindings[key] = (ctx, dict(kwargs))

    def start(self, api_path, module):
        from hermes_cli._startup_fast import project_root_str
        key = Path(api_path).resolve()
        with self._lock:
            if self._closed or key in self._handles:
                raise RuntimeError("plugin lifecycle custody retained or closed")
            binding = self._bindings.get(key)
            if binding is None:
                if key == Path(project_root_str()) / 'plugins/kanban/dashboard/plugin_api.py':
                    # The trusted bundled mount supplies canonical serializers;
                    # the core owns sources and scope, never a plugin-side cache.
                    from hermes_cli.fact_board_startup import start
                    handle = start(module)
                else:
                    hook = getattr(module, "start_legacy", None)
                    if hook is None:
                        return
                    handle = hook()
            else:
                ctx, kwargs = binding
                handle = module.bind_facts(ctx, **kwargs)
            # Store first: even an invalid hook result must not permit a second
            # start after it may already have created resources.
            self._handles[key] = handle
            self._modules[key] = module
            if not callable(getattr(handle, "close", None)):
                raise TypeError("plugin lifecycle requires a closeable handle")

    def stop(self, api_path, *, retain_module=False):
        key = Path(api_path).resolve()
        with self._lock:
            if key in self._handles:
                self._handles[key].close()
                del self._handles[key]
            if not retain_module:
                self._modules.pop(key, None)

    def close(self):
        with self._lock:
            self._closed = True
            failures = []
            for key in tuple(self._handles):
                try:
                    self.stop(key, retain_module=True)
                except Exception as exc:  # health: allow BLE001 -- retain every failed plugin handle while still draining the others
                    failures.append(exc)
            if failures:
                raise ExceptionGroup("plugin lifecycle shutdown incomplete", failures)

    def restart_legacy(self):
        """ASGI app reuse may restart legacy hooks, never reuse facts authority."""
        with self._lock:
            if not self._closed:
                return
            if self._handles:
                raise RuntimeError('plugin lifecycle custody retained')
            if self._bindings:
                raise RuntimeError('plugin lifecycle app has closed authorized bindings')
            self._closed = False
            try:
                for key, module in tuple(self._modules.items()):
                    self.start(key, module)
            except BaseException:
                self.close()
                raise


def lifecycles(app) -> PluginLifecycles:
    """Mount caller holds its existing mount lock; initialize once per app."""
    registry = getattr(app.state, "fact_plugin_lifecycles", None)
    if registry is None:
        registry = PluginLifecycles()
        app.state.fact_plugin_lifecycles = registry
    return registry


@asynccontextmanager
async def lifespan(app):
    """Read on shutdown too: hot mounts can first create a registry after yield."""
    registry = getattr(app.state, "fact_plugin_lifecycles", None)
    if registry is not None and registry._closed:
        await asyncio.to_thread(registry.restart_legacy)
    try:
        yield
    finally:
        registry = getattr(app.state, "fact_plugin_lifecycles", None)
        if registry is not None:
            await asyncio.to_thread(registry.close)


def include_router(app, module, api_path, *, deferred_routes=None, **kwargs):
    """Start before route publication; on publication failure drain this handle."""
    registry = getattr(app.state, "fact_plugin_lifecycles", None)
    if registry is None and getattr(module, "start_legacy", None) is not None:
        registry = lifecycles(app)
    if registry is not None:
        registry.start(api_path, module)
    routes = list(app.router.routes)
    original_lifespan = app.router.lifespan_context
    try:
        app.include_router(module.router, **kwargs)
        if deferred_routes is not None:
            fresh = app.router.routes[len(routes):]
            app.router.routes[:] = routes
            app.router.lifespan_context = original_lifespan
            deferred_routes.append((api_path, fresh, module.router.lifespan_context))
    except BaseException:
        # The mount caller serializes router edits. FastAPI may append several
        # routes before a later one fails, or compose a router lifespan.
        app.router.routes[:] = routes
        app.router.lifespan_context = original_lifespan
        if registry is not None:
            registry.stop(api_path)
        raise
