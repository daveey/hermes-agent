"""HX902: a websockets connection opened without ``logger=`` (the library then logs a handled keepalive drop as
ERROR + traceback; regression for self-heal defect agent-ac35b4d534)."""

from __future__ import annotations

import ast

import pytest

from scripts.code_health.config import RULES_BY_ID, rule_applies
from scripts.code_health.py_rules import CHECKERS, Ctx, canonical_tree


def _hits(src: str) -> list[int]:
    return sorted(set(CHECKERS["HX902"](canonical_tree(ast.parse(src)), Ctx())))


@pytest.mark.parametrize("src, flagged", [
    ("from websockets.sync.client import connect\nconnect('ws://x')\n", True),
    ("def f():\n    from websockets.sync.client import connect\n    with connect('ws://x') as ws:\n        pass\n", True),
    ("import websockets\nasync def f():\n    async with websockets.connect('ws://x') as ws:\n        pass\n", True),
    ("import websockets as w\nasync def f():\n    await w.connect('ws://x', **kw)\n", True),  # a splat is not a logger
    ("import websockets.asyncio.client as c\nasync def f():\n    await c.connect('ws://x')\n", True),
    ("import websockets\nasync def f():\n    async with websockets.serve(h, 'h', 1):\n        pass\n", True),
    ("from websockets.sync.server import serve\nserve(h, 'h', 1)\n", True),
    # optional import with a None fallback: the name is rebound, still the library's connect
    ("try:\n    from websockets.sync.client import connect as ws_connect\nexcept ImportError:\n"
     "    ws_connect = None\nws_connect('ws://x')\n", True),
    # the fix shape
    ("from websockets.sync.client import connect\nconnect('ws://x', logger=ws_logger())\n", False),
    ("import websockets\nasync def f():\n    await websockets.connect('ws://x', logger=lg, **kw)\n", False),
    ("from websockets.sync.server import serve\nserve(h, 'h', 1, logger=ws_logger('server'))\n", False),
    # not websockets
    ("import sqlite3\nsqlite3.connect('db')\n", False),
    ("def connect(u):\n    return u\nconnect('ws://x')\n", False),
])
def test_ws_without_logger_shapes(src, flagged):
    assert bool(_hits(src)) is flagged, src


def test_scope_is_production_code():
    rule = RULES_BY_ID["HX902"]
    assert rule_applies(rule, "hermes_cli/kanban_sync_client.py")
    assert rule_applies(rule, "plugins/platforms/buzz/adapter.py")
    assert not rule_applies(rule, "tests/hermes_cli/test_kanban_feed_keepalive.py")
    assert not rule_applies(rule, "evals/liveness/ws_orphan_reconnect.py")
