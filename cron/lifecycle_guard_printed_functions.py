"""Function-indirected producers of text a startup shell reads (card t_01a1dd07a08dbd73).

``f() { printf 'T'; }; f | bash -l`` prints ``T`` into a login shell, which parses it with alias
names of its own. The definition and the call are separate printed units (a top-level ``;`` ends
a unit, and merging units would change unrelated pipelines' alias scope), so the call's unit alone
holds no printer. Here the definitions a feeding pipeline calls upstream of that shell are resolved
from the same command text and their bodies walked in the nested scope as well.

Bounds and conservative choices:

* every definition of a called name counts (a redefinition may run either body, depending on
  order and control flow), as does every function its bodies name in turn (``f`` calling ``g``);
  each definition is walked once per command, which also ends recursion;
* a name counts as called wherever it appears as a word inside an upstream word (``f``,
  ``"$(f)"``, `` `f` ``, ``xargs f``), and a name the text never defines (a session or
  startup-file function, ``eval``-built text) resolves to nothing: such bodies are outside the
  walked text, as for every other session-defined command;
* a body whose compound cannot be delimited extends to the end of its unit (and the next unit
  when the header ends one, as in ``f()``\\n``{ … }``): it fails closed by walking more text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cron.lifecycle_guard_nested_shells import _PrintedUnit

# Characters that may continue a function name (bash accepts most non-metacharacters).
_NAME_CHARS = r"A-Za-z0-9_.:@%+\-/,^~"


def _definition_header(events: Sequence[tuple[str, str]], index: int) -> Optional[tuple[str, int]]:
    """``(name, index of the body's first event)`` when a function definition starts at *index*:
    ``function NAME [( )]`` or ``NAME ( )``."""
    role, word = events[index]
    if role != "word":
        return None
    following = list(events[index + 1:index + 3])
    if word == "function" and not getattr(word, "quoted", False) and following and (
            following[0][0] == "word"):
        start = index + 2
        if list(events[start:start + 2]) == [("open", "("), ("close", ")")]:
            start += 2
        return following[0][1], start
    after_keyword = index and events[index - 1] == ("word", "function") and not getattr(
        events[index - 1][1], "quoted", False)
    if following == [("open", "("), ("close", ")")] and not after_keyword:
        return word, index + 3
    return None


def _compound_events(events: Sequence[tuple[str, str]], start: int) -> list[tuple[str, str]]:
    """The events of the compound command starting at *start*; the rest of *events* when it does
    not open one there or never closes (fail closed)."""
    while start < len(events) and events[start][0] == "newline":
        start += 1
    if start >= len(events) or events[start][0] != "open":
        return list(events[start:])
    depth = 0
    for position in range(start, len(events)):
        role = events[position][0]
        depth += (role == "open") - (role == "close")
        if depth <= 0:
            return list(events[start:position + 1])
    return list(events[start:])


class FunctionProducers:
    """The function definitions in a command's printed units, resolved on first use."""

    def __init__(self, units: Sequence[_PrintedUnit]) -> None:
        self._units = units
        self._definitions: Optional[dict[str, list[_PrintedUnit]]] = None
        self._pattern: Optional[re.Pattern[str]] = None
        self._walked: set[int] = set()

    def _load(self) -> dict[str, list[_PrintedUnit]]:
        if self._definitions is not None:
            return self._definitions
        from cron.lifecycle_guard_nested_shells import _is_heredoc_opener, _PrintedUnit

        definitions: dict[str, list[_PrintedUnit]] = {}
        for unit_index, unit in enumerate(self._units):
            for index in range(len(unit.events)):
                header = _definition_header(unit.events, index)
                if header is None:
                    continue
                name, start = header
                source = unit
                if start >= len(unit.events) and unit_index + 1 < len(self._units):
                    source, start = self._units[unit_index + 1], 0  # `f()`⏎`{ … }`
                body = _PrintedUnit()
                body.events = _compound_events(source.events, start)
                if any(role == "word" and _is_heredoc_opener(word) for role, word in body.events):
                    body.bodies = list(source.bodies)
                if body.events and name:
                    definitions.setdefault(name, []).append(body)
        self._definitions = definitions
        if definitions:
            names = "|".join(re.escape(name) for name in sorted(definitions, key=len, reverse=True))
            self._pattern = re.compile(rf"(?<![{_NAME_CHARS}])(?:{names})(?![{_NAME_CHARS}])")
        return definitions

    def called_texts(self, events: Sequence[tuple[str, str]]) -> list[str]:
        """Texts (bodies with their heredoc bodies) of the not yet walked definitions that
        *events* call, directly or through other bodies."""
        if not any(role == "word" for role, _word in events):
            return []
        definitions = self._load()
        if not definitions or self._pattern is None:
            return []
        texts: list[str] = []
        pending = [word for role, word in events if role in ("word", "data")]
        while pending:
            for name in self._pattern.findall(pending.pop()):
                for body in definitions.get(name, ()):
                    if id(body) in self._walked:
                        continue
                    self._walked.add(id(body))
                    texts.append("\n".join([body.text(), *body.bodies]))
                    pending.extend(word for role, word in body.events if role in ("word", "data"))
        return texts
