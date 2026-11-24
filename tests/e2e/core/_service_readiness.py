"""Listener readiness precedes standalone service readiness; retry only its explicit fence."""
from __future__ import annotations

import httpx

from tests.waiting import wait_until


def service_response(client: httpx.Client, path: str, *, timeout: float = 120) -> httpx.Response:
    """Return the first non-warmup response, leaving auth and application errors visible."""
    def probe():
        response = client.get(path)
        if response.status_code == 503 and response.headers.get("content-type", "").startswith("application/json"):
            if response.json() == {"detail": "Backend services warming"}:
                return None
        return response

    response = wait_until(probe, timeout=timeout, interval=0.1, what=f"service warmup before GET {path}")
    assert response is not None
    return response
