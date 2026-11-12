"""Encode JSON list responses in the pool thread, not on the event loop. [daveey]

FastAPI serializes a plain dict/list return value with ``jsonable_encoder``, a
pure-Python recursive walk that runs on the event loop: for a 200-row
``/api/profiles/sessions`` page that walk is most of the request's CPU. A
JSON-native payload (str/int/float/bool/None/dict/list/tuple) needs no such walk:
``json.dumps`` renders it as is, and for such a payload ``jsonable_encoder``
returns an equal value, so ``JSONResponse(content=payload)`` built off the loop
yields the same status, headers and body bytes as FastAPI's default path.
"""
import functools

from starlette.responses import JSONResponse, Response


def native_json_response(payload):
    """``JSONResponse(content=payload)``, or ``payload`` unchanged when it is not JSON-native.

    Call it from the worker thread that produced ``payload``. A payload that
    ``json.dumps`` refuses (datetime, set, Path, dataclass, Enum, NaN, ...) raises
    ``TypeError``/``ValueError`` here and comes back unchanged, so FastAPI's
    ``jsonable_encoder`` path handles it exactly as before. A ``Response`` is
    returned as is.
    """
    if isinstance(payload, Response):
        return payload
    try:
        return JSONResponse(content=payload)
    except (TypeError, ValueError):
        return payload


def per_request_response(result):
    """A fresh ``Response`` for this request from a possibly shared encoded result.

    A coalesced read or cache hands one result to several waiting requests.
    FastAPI sets ``response.background`` on a returned Response, so one shared
    ``JSONResponse`` object would carry one request's background tasks into
    another; each request gets its own object instead.
    The shared part is the rendered body; the per-request ``Response`` built here
    is a constant-time wrapper with the same status, content-type and
    content-length headers. Anything else (a raw payload that took the fallback,
    a route's own ``Response``) is returned unchanged.
    """
    if type(result) is not JSONResponse:
        return result
    return Response(content=result.body, status_code=result.status_code, media_type=result.media_type)


def encoded_in_thread(func):
    """Wrap a synchronous read so its result is JSON-encoded in the same (pool) thread.

    ``functools.wraps`` keeps the signature and annotations FastAPI and
    ``coalesced_read`` read through ``__wrapped__``, so query validation is
    unchanged. Run the returned callable in a worker thread and pass its result
    through :func:`per_request_response` on the loop.
    """
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        return native_json_response(func(*args, **kwargs))
    return wrapped
