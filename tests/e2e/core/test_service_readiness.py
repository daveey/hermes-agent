"""Readiness probes retry warmup, not authentication failures or application faults."""
from __future__ import annotations

import httpx
import pytest

from tests.e2e.core._service_readiness import service_response


@pytest.mark.parametrize("status,body", [
    (200, {"ready": True}),
    (401, {"detail": "unauthenticated"}),
    (503, {"detail": "storage unavailable"}),
])
def test_warmup_wait_preserves_first_application_response(status, body):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"detail": "Backend services warming"})
        return httpx.Response(status, json=body)

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        response = service_response(client, "/api/status")
    assert response.status_code == status and response.json() == body
    assert len(requests) == 2


def test_unfinished_warmup_is_bounded():
    def handler(request):
        return httpx.Response(503, json={"detail": "Backend services warming"})

    with httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AssertionError, match="service warmup before GET /api/status"):
            service_response(client, "/api/status", timeout=0)
