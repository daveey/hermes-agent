"""The interactive dashboard harness waits for service warmup too."""
from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest

from tests.e2e.core.dashboard import _issue_helpers


@pytest.mark.parametrize("status", [200, 401, 503])
def test_pty_boot_retries_only_service_warmup(monkeypatch, tmp_path, status):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"detail": "Backend services warming"})
        return httpx.Response(status, text='__HERMES_SESSION_TOKEN__ = "fixture-token"')

    client_type = httpx.Client
    monkeypatch.setattr(_issue_helpers.httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(handle)))
    proc = SimpleNamespace(stdout=io.StringIO("HERMES_DASHBOARD_READY port=12345\n"),
                           poll=lambda: None, returncode=None)
    monkeypatch.setattr(_issue_helpers.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(_issue_helpers.PtyDashboard, "close", lambda self: None)
    sb = SimpleNamespace(root=tmp_path, home=tmp_path, env=lambda extra: extra)
    server = _issue_helpers.PtyDashboard.__new__(_issue_helpers.PtyDashboard)
    try:
        if status == 200:
            server.__init__(sb, tmp_path / "boot.log", tty=False)
            assert server.token == "fixture-token"
        else:
            with pytest.raises(AssertionError, match=f"no session token: {status}"):
                server.__init__(sb, tmp_path / "boot.log", tty=False)
        assert len(calls) == 2
    finally:
        server.http.close()
        server._log.close()
