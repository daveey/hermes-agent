"""Bind native display surfaces before standalone service warmup, fail closed elsewhere."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import logging
import sys
import time

from starlette.responses import JSONResponse

_log = logging.getLogger(__name__)
_defer_plugin_api_sweep = False


def defer_plugin_api_sweep():
    """Only a headless launcher before web_server import may defer the sweep."""
    global _defer_plugin_api_sweep
    if 'hermes_cli.web_server' in sys.modules:
        return False
    _defer_plugin_api_sweep = True
    return True


_DISPLAY_PATHS = frozenset(('/api/health', '/api/profiles/sessions',
                            '/api/profiles/sessions/sidebar', '/api/profiles/sessions/stamps'))
# Token-gated process identity (pid, launch nonce, runtime marker), fixed before the listener hands
# off. The always-on roller binds native sessions to this incarnation before it routes the proxy;
# held behind service warmup, the proxy stayed on the predecessor ~4 s after the successor's real
# rows were ready (run71 natural canary: direct 4.95 s, proxy 9.41 s from unit start).
_IDENTITY_PATHS = frozenset(('/api/host/identity', '/api/ssh/ownership'))
_WARMUP_PATHS = _DISPLAY_PATHS | _IDENTITY_PATHS


class BootServicesMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            ready = getattr(scope['app'].state, 'boot_services_ready', None)
            if ready is not None and not ready.is_set():
                if scope['type'] == 'websocket':
                    await receive()  # consume websocket.connect before a retryable close
                    await send({'type': 'websocket.accept'})
                    await send({'type': 'websocket.close', 'code': 1013})
                    return
                if scope['method'] != 'GET' or scope['path'] not in _WARMUP_PATHS:
                    response = JSONResponse({'detail': 'Backend services warming'}, status_code=503,
                                            headers={'Retry-After': '1'})
                    await response(scope, receive, send)
                    return
            # State yielded by plugin router lifespans entered after app startup (deferred sweep).
            carried = getattr(scope['app'].state, 'plugin_lifespan_state', None)
            if carried:
                scope.setdefault('state', {}).update(carried)
        await self.app(scope, receive, send)


def _sweep_plugin_apis(app):
    """Prepare routes off-loop, retaining lifecycle custody but not publication."""
    from hermes_cli import web_server_dashboard as dashboard
    from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set
    from hermes_cli.web_server import _get_dashboard_plugins
    wall, cpu = time.perf_counter(), time.thread_time()
    pending = []
    with dashboard._launch_profile_scope():
        enabled, disabled = _get_enabled_set(), _get_disabled_set()
        for plugin in _get_dashboard_plugins():
            with dashboard._plugin_api_mount_lock:
                captured = []
                before = list(app.router.routes)
                if dashboard._mount_one_plugin_api(app, plugin, enabled, disabled, deferred_routes=captured):
                    if not captured:  # Hosted proxy routes have no in-process router lifespan.
                        captured.append((None, app.router.routes[len(before):], None))
                        app.router.routes[:] = before
                    pending.extend((plugin['name'], *entry) for entry in captured)
    _log.info('Deferred plugin API sweep: mounted=%d wall=%.3fs thread_cpu=%.3fs',
              len(pending), time.perf_counter() - wall, time.thread_time() - cpu)
    return pending


@asynccontextmanager
async def _services_with_plugin_apis(app, services):
    owed = getattr(app.state, 'plugin_api_sweep_owed', False)
    # Legacy start launches kanban-board-prewarm BEFORE services' hold_bind_for_prewarm joins it.
    pending = await asyncio.to_thread(_sweep_plugin_apis, app) if owed else []
    async with services(app), AsyncExitStack() as stack:
        fresh, carried = [], {}
        if pending:
            # The eager path hands a router lifespan's yielded state to every request through
            # FastAPI's merged lifespan. That lifespan has already started here, so the
            # middleware copies this dict into each request instead; it ends with the lifespans.
            app.state.plugin_lifespan_state = carried
            stack.callback(carried.clear)
        for name, api_path, routes, context in pending:
            try:
                if context is not None:
                    state = await stack.enter_async_context(context(app))
                    if state is not None:
                        carried.update(state)
            except Exception:  # health: allow BLE001 -- third-party lifespan failure must not prevent other plugins from starting
                _log.warning('Failed to enter plugin %s router lifespan', name, exc_info=True)
                # Remembered for the app's life: without it the first request would reach the
                # hot-mount fallback, import the plugin again and serve it with no lifespan.
                if not hasattr(app.state, 'plugin_api_refused'):
                    app.state.plugin_api_refused = set()
                app.state.plugin_api_refused.add(name)
                registry = getattr(app.state, 'fact_plugin_lifecycles', None)
                if registry is not None:
                    try:
                        # close() is the plugin's own blocking code: keep it off the event loop.
                        await asyncio.to_thread(registry.stop, api_path)
                    except Exception:  # health: allow BLE001 -- a third-party close() failure must not fail warmup for the other plugins
                        # The registry keeps the handle, so shutdown tries the close again.
                        _log.warning('Failed to stop plugin %s after its router lifespan failed', name, exc_info=True)
                continue
            fresh.extend(routes)
        if owed:
            from hermes_cli.web_server_dashboard import _plugin_api_hot_mount
            routes = app.router.routes
            at = next((i for i, route in enumerate(routes)
                       if getattr(route, 'endpoint', None) is _plugin_api_hot_mount), len(routes))
            routes[:] = routes[:at] + fresh + routes[at:]
            app.openapi_schema = None
            app.state.plugin_api_sweep_owed = False
        yield


@asynccontextmanager
async def services_lifespan(app, services):
    listener = getattr(app.state, 'http_listener_ready', None)
    if (listener is None or getattr(app.state, 'sessions_facts', None) is None
            or getattr(app.state, 'gateway_runner', None) is not None):
        async with _services_with_plugin_apis(app, services):
            yield
        return
    ready, stop = asyncio.Event(), asyncio.Event()
    app.state.boot_services_ready = ready

    async def warm():
        await listener.wait()
        async with _services_with_plugin_apis(app, services):
            ready.set()
            await stop.wait()

    task = asyncio.create_task(warm(), name='standalone-service-warmup')
    def report_failure(done):
        if not done.cancelled() and (error := done.exception()) is not None:
            import logging
            logging.getLogger(__name__).error('Standalone service warmup failed; routes remain unavailable',
                                             exc_info=(type(error), error, error.__traceback__))
    task.add_done_callback(report_failure)
    try:
        yield
    finally:
        ready.clear()
        stop.set()
        # Before the listener starts no service owns resources. Once it starts,
        # do not cancel a half-entered lifespan and lose its shutdown custody.
        try:
            if not listener.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            else:
                cancelled = False
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # Cancellation of the serving lifespan cannot abandon a
                        # half-entered service owner or release sessions first.
                        cancelled = True
                task.result()
                if cancelled:
                    raise asyncio.CancelledError
        finally:
            app.state.boot_services_ready = None
