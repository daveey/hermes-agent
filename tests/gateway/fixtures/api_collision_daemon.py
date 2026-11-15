"""Serve the real API on a socket whose parent retains custody across SIGKILL."""
import os
import runpy
import socket
import sys

from aiohttp import web
from gateway.platforms import api_server


async def inherited_site(runner, host, port, *, log_tag):
    assert host == '127.0.0.1' and port == 0, (host, port)
    listener = socket.socket(fileno=int(os.environ['HERMES_TEST_API_SOCKET']))
    site = web.SockSite(runner, listener)
    await site.start()
    return site


api_server.start_tcp_site = inherited_site
if len(sys.argv) == 2:
    fixture = sys.argv.pop()
    runpy.run_path(fixture, run_name='__main__')
else:
    runpy.run_module('gateway.run', run_name='__main__')
