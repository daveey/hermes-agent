"""Connection-owned SQLite callbacks that can be refreshed with active cursors."""
from __future__ import annotations

import sqlite3
import weakref
from collections.abc import Callable
from contextvars import ContextVar


# Strings only: never retain an exception traceback (and its connection).
_INNER: ContextVar[tuple[str, str, str] | None] = ContextVar("containment_udf_error", default=None)


def clear_error() -> None:
    """Start a sync board round without a diagnostic from an earlier round."""
    _INNER.set(None)


def error_suffix(exc: BaseException) -> str:
    """Consume callback evidence only for SQLite's opaque UDF error."""
    inner = _INNER.get()
    clear_error()
    if inner is None or "user-defined function raised exception" not in str(exc):
        return ""
    kind, message, name = inner
    return f"; inner: {kind}: {message} in {name}"


class _Function:
    def __init__(self, conn: sqlite3.Connection, callback: Callable, name: str) -> None:
        self.conn = conn
        self.callback = callback
        self.name = name

    def __call__(self, *args):
        try:
            return self.callback(*args)
        except BaseException as exc:
            _INNER.set((type(exc).__name__, str(exc).replace("\n", "\\n").replace("\r", "\\r")[:2048], self.name))
            raise


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
    function = _Function(conn, callback, name)
    conn.create_function(name, arity, function)
    _FUNCTIONS[key] = function
