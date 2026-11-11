"""``commands.catalog`` runs on the RPC pool, never inline on the socket's ordered request lane.

Desktop sends it in the burst right after ``session.create`` and sends ``prompt.submit`` a moment
later on the same socket. The catalog walks skills, plugins and quick commands for the session's
profile and workspace (measured 1.9 s on a fresh session, 0.6 s warm, 5.5 s under load), so run
inline it held the new chat's first ``prompt.submit`` behind it for that long."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class _Transport:
    def __init__(self):
        self.frames = queue.Queue()

    def write(self, frame):
        self.frames.put(frame)
        return True


def test_commands_catalog_is_a_long_handler():
    from tui_gateway import server

    assert "commands.catalog" in server._LONG_HANDLERS


def test_slow_commands_catalog_does_not_hold_the_request_after_it(monkeypatch):
    from tui_gateway import command_discovery, server

    release = threading.Event()

    def slow_catalog(**_kwargs):
        release.wait(10)
        return {"pairs": [], "sub": {}, "canon": {}, "categories": []}

    monkeypatch.setattr(command_discovery, "command_catalog", slow_catalog)
    transport = _Transport()
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(server, "_pool", pool)
        started = time.monotonic()
        assert server.dispatch({"id": "cat", "method": "commands.catalog", "params": {}}, transport) is None
        # The next request on the same lane answers while the catalog is still running.
        follow = server.dispatch({"id": "ping", "method": "gateway.ping", "params": {}}, transport)
        assert time.monotonic() - started < 2
        assert follow is not None and follow.get("id") == "ping"
        assert transport.frames.empty()
        release.set()
        reply = transport.frames.get(timeout=10)

    assert reply["id"] == "cat"
    assert "result" in reply
