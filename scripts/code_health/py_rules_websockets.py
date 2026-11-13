"""HX902: a websockets connection opened without ``logger=`` (registered in ``py_rules.CHECKERS``).

websockets logs a peer that stops answering pings as ``ERROR keepalive ping failed`` + traceback from its own
keepalive thread, even when the caller handles the drop (reconnect, end the stream). Every Hermes call site
passes ``logger=hermes_cli.ws_logging.ws_logger()`` so that handled drop is one INFO line.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.code_health.py_ast import Ctx, _call_name

# Canonical names (``canonical_tree`` resolves import aliases) of the calls that open a websockets connection.
_OPENERS = frozenset({
    "websockets.connect", "websockets.serve",
    "websockets.client.connect", "websockets.server.serve",
    "websockets.sync.client.connect", "websockets.sync.server.serve",
    "websockets.asyncio.client.connect", "websockets.asyncio.server.serve",
    "websockets.legacy.client.connect", "websockets.legacy.server.serve",
})


def _imported_openers(tree: ast.Module) -> dict[str, str]:
    """Local names bound by ``from websockets... import connect/serve``. ``canonical_tree`` leaves a name alone
    when the module also rebinds it (``except ImportError: connect = None``), so match those by name."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "websockets":
            for alias in node.names:
                if f"{node.module}.{alias.name}" in _OPENERS:
                    names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def ws_without_logger(tree: ast.Module, ctx: Ctx) -> Iterable[int]:
    local = _imported_openers(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or any(kw.arg == "logger" for kw in node.keywords):
            continue
        if _call_name(node) in _OPENERS or (isinstance(node.func, ast.Name) and node.func.id in local):
            yield node.lineno
