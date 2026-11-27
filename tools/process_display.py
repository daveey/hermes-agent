"""The command a background process should be *shown* as.

Host ``tool_request`` middleware may prefix a terminal command before it runs:
hermes-mods' ``kanban-worker-nice`` plugin adds a niceness floor as two lines,
``# kanban-worker-nice terminal floor`` and one ``renice`` line. The registry
records the command that actually ran, so session outcomes, the sidebar and the
status panel labelled such a process ``# kanban-worker-nice terminal floor if
[ "$(nice)" -lt 10 ]; then renice ...`` instead of what the agent asked for.

Contract for injecting middleware: mark the prefix with a ``# <source> terminal
floor`` comment line followed by exactly one shell line. Only leading blocks of
that shape are dropped, so an agent's own comments and commands stay intact.
[daveey]
"""

from __future__ import annotations

import re
from typing import Any

INJECTED_PREAMBLE_RE = re.compile(r"\A(?:# [A-Za-z0-9_.-]+ terminal floor\r?\n[^\n]*(?:\n|\Z))+")


def display_command(command: Any) -> str:
    """``command`` without a host middleware's injected preamble (``''`` for a missing command)."""
    return INJECTED_PREAMBLE_RE.sub("", str(command or ""), count=1)
