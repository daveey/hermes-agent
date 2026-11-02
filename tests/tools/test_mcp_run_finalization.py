"""MCP transport recovery must not run after interpreter teardown has begun."""

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tools.environments.local import build_subprocess_env
from tools.mcp_tool_server_run import MCPServerRunMixin


@pytest.mark.parametrize("finalizing", [False, True])
def test_transport_failure_recovers_only_before_finalization(monkeypatch, finalizing):
    server = MCPServerRunMixin()
    server._prepare_run = AsyncMock(return_value=True)
    server._is_http = lambda: False
    failure = RuntimeError("no running event loop")
    server._run_stdio = AsyncMock(side_effect=failure)
    server._on_transport_error = AsyncMock(return_value=False)
    monkeypatch.setattr(sys, "is_finalizing", lambda: finalizing)

    asyncio.run(server.run({}))

    assert server.session is None
    assert server._stdio_child_pids == set()
    if finalizing:
        server._on_transport_error.assert_not_called()
    else:
        assert server._on_transport_error.await_count == 1
        assert server._on_transport_error.await_args.args[0] is failure


def test_retained_run_coroutine_is_quiet_at_real_interpreter_exit(tmp_path):
    """Force the late-GC boundary, not a mock of sys.is_finalizing.

    The transport models an async context manager failing to unwind without its
    event loop. The real run/error-classification stack must not try late imports.
    """
    code = textwrap.dedent('''
        import gc
        from tools.mcp_tool_server_run import MCPServerRunMixin

        class Suspend:
            def __await__(self):
                yield

        class Server(MCPServerRunMixin):
            async def _prepare_run(self, config):
                return True
            def _is_http(self):
                return False
            async def _run_stdio(self, config):
                try:
                    await Suspend()
                finally:
                    raise RuntimeError("no running event loop")

        server = Server()
        server.coro = server.run({})
        server.coro.send(None)
        gc.disable()
        print("suspended", flush=True)
    ''')
    env = build_subprocess_env()
    env["HERMES_HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2], env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "suspended"
    assert result.stderr == ""
