"""Process scans tolerate editor MCP peers exiting between enumeration and argv reads."""
import os
import subprocess
import sys

import psutil
import pytest

from tests.acp_adapter.test_gateway_editor_mcp import _peers_running
from tests.waiting import wait_until


@pytest.mark.platforms("linux")
@pytest.mark.parametrize('reap', [False, True], ids=['zombie', 'reaped'])
def test_peer_exiting_during_scan_does_not_hide_a_live_peer(tmp_path, monkeypatch, reap):
    script = tmp_path / 'peer.py'
    script.write_text('import sys\nsys.stdin.read()\n')
    with subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE) as exiting, \
            subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE) as live:
        original = psutil.Process.cmdline
        # Popen's exec handshake can precede argv publication on a loaded runner.
        wait_until(lambda: all(str(script) in original(psutil.Process(p.pid))
                               for p in (exiting, live)), timeout=30, what='peer argv published')
        assert {exiting.pid, live.pid} <= {p.pid for p in _peers_running(os.getpid())}
        observed = []

        def cmdline(process):
            if process.pid == exiting.pid:
                exiting.kill()
                if reap:
                    exiting.wait(timeout=30)
                else:
                    wait_until(lambda: process.status() == psutil.STATUS_ZOMBIE,
                               timeout=30, what='peer exited but not yet reaped')
                observed.append(process.pid)
            return original(process)

        monkeypatch.setattr(psutil.Process, 'cmdline', cmdline)
        peers = {p.pid for p in _peers_running(os.getpid())}
        assert observed == [exiting.pid]
        assert exiting.pid not in peers
        assert live.pid in peers
        assert live.stdin is not None
        live.stdin.close()
        live.wait(timeout=30)
