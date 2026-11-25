"""Board urllib fallback, with bounded reads and the client's patch seams."""
from __future__ import annotations

import http.client
from typing import Optional


def call(hub, method: str, path: str, data: Optional[bytes], timeout: Optional[float] = None, *,
         max_response_bytes: Optional[int] = None) -> dict:
    from hermes_cli import kanban_sync_client as client

    timeout = hub._remaining(timeout)
    req = client.urllib.request.Request(hub.ident.url + path, data=data, method=method, headers=hub._headers())
    # Keep facade lookups late-bound: deadline, urllib, clock and read seams are
    # shared with kept-alive callers and existing transport monkeypatches.
    open_request = (client.urllib.request.build_opener(client._DeadlineRequests(hub._remaining)).open
                    if hub.deadline is not None else client.urllib.request.urlopen)
    if max_response_bytes is not None:
        handlers: list[client.urllib.request.BaseHandler] = [client.BoundedRedirects(max_response_bytes)]
        if hub.deadline is not None:
            handlers.append(client._DeadlineRequests(hub._remaining))
        open_request = client.urllib.request.build_opener(*handlers).open
    started = client.time.monotonic()
    try:
        try:
            with open_request(req, timeout=timeout) as resp:
                return client.decode_json(client.read_response(resp, max_response_bytes))
        except client.urllib.error.HTTPError as exc:
            with exc:
                text = client.read_response(exc, max_response_bytes, deterministic=False).decode("utf-8", "replace")[:500]
            if exc.code == 410:
                raise client.Resync(text) from exc
            raise client.HubError(exc.code, text) from exc
    except http.client.HTTPException as exc:
        # urllib leaves malformed status lines and truncated bodies unwrapped,
        # including failures while reading an HTTPError's response body.
        error = client.urllib.error.URLError(exc)
        client.note_attempt(error, hub.deadline, started, client.time.monotonic())
        raise error from exc
    except Exception as exc:  # health: allow BLE001 -- stamps diagnostics, then re-raises the same error
        client.note_attempt(exc, hub.deadline, started, client.time.monotonic())
        raise
