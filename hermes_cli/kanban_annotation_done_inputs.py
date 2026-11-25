"""Explicit semantic inputs for registered done checks (same contract as waiting-source ``inputs``). [daveey]

``register_done_check(..., inputs=callable)`` declares that ``inputs()`` returns a JSON-portable value that changes
whenever ``fn(card)`` could answer differently for an unchanged card: every database, clock and other dependency
must be in it. ``[]`` / ``{}`` are valid and mean "no dependency beyond the card". The callback may read databases
in the native holder but never waits on events. A check without a declaration, or whose callback raises, returns
None or yields a non-portable value, makes the whole key a fresh unequal ``object()``: callers must then recompute
(or fall back to raw signatures). A declaration is bound to the exact ``fn`` it was registered with, so a replacement
closure under the same name is undeclared until it declares itself.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("hermes_cli.kanban_session_activity")
_UNDECLARED = "undeclared"
# name -> (fn, inputs)
_declared: dict[str, tuple[Any, Any]] = {}


def declaration(name: str, fn, inputs) -> None:
    """Bind ``inputs`` to exactly ``fn``; ``fn`` or ``inputs`` None (or not callable) removes the declaration."""
    if fn is None or inputs is None or not callable(inputs):
        _declared.pop(name, None)
    else:
        _declared[name] = (fn, inputs)


def dispose(name: str, fn) -> None:
    """Remove ``name``'s declaration only while it still belongs to ``fn`` (replacement-safe)."""
    current = _declared.get(name)
    if current is not None and current[0] is fn:
        _declared.pop(name, None)


def inputs(checks: dict[str, Any]) -> tuple[list, bool]:
    """``([[name, code id, inputs or "undeclared"]...], complete)``; never raises."""
    from hermes_cli import kanban_annotation_host as host

    out: list = []
    complete = True
    for name in sorted(checks):
        fn = checks[name]
        declared = _declared.get(name)
        if declared is None or declared[0] is not fn:
            out.append([name, host._code_id(fn), _UNDECLARED])
            complete = False
            continue
        try:
            value = declared[1]()
            if value is None:
                raise ValueError("None is not an explicit declaration")
            out.append([name, host._code_id(fn), host._portable(value)])
        except Exception:  # health: allow BLE001 -- a provider callback may raise anything; the key is then unknown
            _log.debug("done check inputs failed for %s", name, exc_info=True)
            out.append([name, host._code_id(fn), _UNDECLARED])
            complete = False
    return out, complete


def key(checks: dict[str, Any]) -> object:
    """Comparable key when every check is explicitly declared and portable, else a fresh unequal object."""
    values, complete = inputs(dict(checks))
    # Code identity alone does not distinguish two closures from the same source: keep the fn objects too.
    return (tuple(sorted(checks.items(), key=lambda kv: kv[0])), values) if complete else object()
