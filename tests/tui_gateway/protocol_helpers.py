"""Explicit stdio RPC capture fixture for protocol tests."""
import io

import pytest


@pytest.fixture()
def capture(server, monkeypatch):
    """Redirect the explicitly selected stdio RPC channel to a StringIO."""
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", True)
    buf = io.StringIO()
    server._real_stdout = buf
    return server, buf
