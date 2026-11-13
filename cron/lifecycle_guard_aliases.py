"""Which shell aliases may change what the lifecycle guard's command scan sees.

An alias can rename a heredoc consumer (``alias python3=bash``) or expand to a keyword prefix
(``alias t='time '``), so both helpers here fail closed when the names are unknown.
"""

from __future__ import annotations

import re
from typing import Optional


def _may_define_alias(text: str) -> bool:
    """Whether *text* may define or enable an alias (``alias T=' '``, ``BASH_ALIASES[T]=…``,
    ``shopt -s expand_aliases``), even quote-split (``al''ias``) or ANSI-C quoted (``$'…'``).
    An alias can expand to a keyword prefix, so ``T ( bash big.sh )`` may run a subshell.

    Deliberately broad: any "alias" substring (a path, ``BASH_ALIASES``, prose) counts. A false
    positive only makes call-argument paths executed again (fail closed); narrowing it to whole
    words or ``alias NAME=`` would let quote-split or ``$'…'`` spellings fail open. Do not tighten."""
    # Dropping every quote and backslash rejoins quote-split words (`al''ias`, `al\ias`).
    return "alias" in re.sub(r"[\"'\\\\]", "", text).lower() or "$'" in text


def _session_aliases_shadow_consumers(session_aliases: Optional[frozenset[str]]) -> bool:
    """Whether aliases the command's shell already holds may rename a heredoc consumer, wrapper
    or shell (``alias python3=bash`` runs a "Python" body as shell). ``None`` means the names
    are unknown, so any name may be shadowed."""
    from tools.shell_heredoc import shadows_heredoc_consumer

    if session_aliases is None:
        return True
    return any(not isinstance(name, str) or shadows_heredoc_consumer(name)
               for name in session_aliases)
