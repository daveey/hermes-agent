"""Bind native display surfaces before standalone service warmup, fail closed elsewhere."""
import asyncio
from contextlib import asynccontextmanager

from starlette.responses import JSONResponse

_DISPLAY_PATHS = frozenset(('/api/health', '/api/profiles/sessions',
                            '/api/profiles/sessions/sidebar', '/api/profiles/sessions/stamps'))


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
                if scope['method'] != 'GET' or scope['path'] not in _DISPLAY_PATHS:
                    response = JSONResponse({'detail': 'Backend services warming'}, status_code=503,
                                            headers={'Retry-After': '1'})
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


@asynccontextmanager
async def services_lifespan(app, services):
    listener = getattr(app.state, 'http_listener_ready', None)
    if (listener is None or getattr(app.state, 'sessions_facts', None) is None
            or getattr(app.state, 'gateway_runner', None) is not None):
        async with services(app):
            yield
        return
    ready, stop = asyncio.Event(), asyncio.Event()
    app.state.boot_services_ready = ready

    async def warm():
        await listener.wait()
        async with services(app):
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
