"""Connection-owned SQLite callbacks that can be refreshed with active cursors."""
from __future__ import annotations

import sqlite3
import weakref
from collections.abc import Callable


class _Function:
    def __init__(self, conn: sqlite3.Connection, callback: Callable) -> None:
        self.conn = conn
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


# sqlite3.Connection itself is not weak-referenceable. SQLite owns the callable
# until close (or collection); weak values avoid retaining connections globally
# and prevent a recycled connection id from inheriting another connection's UDF.
_FUNCTIONS: weakref.WeakValueDictionary[tuple[int, str, int], _Function] = weakref.WeakValueDictionary()


def register(conn: sqlite3.Connection, name: str, arity: int, callback: Callable) -> None:
    """Replace Python callback semantics, never re-register an existing SQL UDF.

    SQLite rejects create_function replacement while ANY statement is active.
    Refreshing the callable also preserves guard verifier/snapshot transitions.
    """
    key = (id(conn), name, arity)
    function = _FUNCTIONS.get(key)
    if function is not None:
        function.callback = callback
        return
    function = _Function(conn, callback)
    conn.create_function(name, arity, function)
    _FUNCTIONS[key] = function
