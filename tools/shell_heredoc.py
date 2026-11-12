"""Conservative heredoc masking for shell-command scanners ('&' guard, blocked-command checks,
cron lifecycle_guard) that false-positive on heredoc *bodies*. Stripping every body is unsafe the
other way (a fake ``<<`` in quotes can swallow an operator; unquoted bodies expand; ``bash <<'EOF'``
executes), so a body is masked ONLY when every delimiter is quoted, every heredoc has an exact
terminator line, the owning simple command (after transparent wrappers such as ``timeout 20`` are
peeled; optionally the first command of a ``name() {`` body) is an allowlisted non-shell
interpreter, and no list operator follows the heredoc. Otherwise the command is returned
untouched: a false positive is acceptable, hiding shell syntax from a guard is not. A quoted body
that stays visible (``ssh h 'bash -s' <<'R'``) is literal text, so data-consumer heredocs nested in
it are judged by the same rules.
Masked bodies keep their newline count (re.MULTILINE)."""

from __future__ import annotations

import re

# Non-shell interpreters whose quoted heredoc bodies are data for THAT interpreter, judged on the
# command the owner actually runs: VAR=... assignments and transparent wrappers (``timeout 20``,
# ``nice``, ``env VAR=1``, ``command``) are peeled, a path prefix is allowed, positional arguments
# may follow. Narrow on purpose: unmatched = visible.
_INERT_HEREDOC_CONSUMER_RE = re.compile(r"^(?:python(?:3(?:\.\d+)*)?|osascript|cat)$", re.IGNORECASE)
# Wrappers that run their operand with the heredoc on its stdin. Excluded: ``su``/``runuser`` (the
# operand is a USER and a shell reads stdin) and every other privilege/namespace wrapper.
_INERT_HEREDOC_WRAPPERS = frozenset({
    "timeout", "nice", "ionice", "nohup", "setsid", "stdbuf", "env", "command", "exec",
    "eatmydata", "sudo", "doas",
})
# A wrapper option that hands a STRING to a shell (``env -S 'bash -s'``) or starts one (``sudo
# -s``) makes the peeled word unreliable: keep the body visible.
_SHELL_STARTING_WRAPPER_OPTIONS = frozenset({
    "-S", "--split-string", "-c", "--command", "-s", "--shell", "-i", "--login",
})
_ASSIGNMENT_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# ``name() {`` / ``function name {`` opening a function body on the heredoc's own line.
_FUNCTION_HEADER_RE = re.compile(
    r"\s*(?:function\s+[A-Za-z_][\w.:-]*\s*(?:\(\s*\))?|[A-Za-z_][\w.:-]*\s*\(\s*\))\s*\{\s+")
# Nested heredocs inside a quoted outer body are classified recursively, this deep at most.
_MAX_NESTED_HEREDOC_DEPTH = 4
# A command that (re)defines a consumer or wrapper name as a function or alias anywhere
# (``python3() { bash; }``) may run a shell where the peeled word says "python": mask nothing.
_CONSUMER_SHADOW_RE = re.compile(
    r"(?:^|[\s;&|(){{}}])(?:(?:function\s+)?{names}\s*\(\s*\)|function\s+{names}\s+\{{"
    r"|alias\s+{names}=)".format(
        names=r"(?:python[\d.]*|osascript|cat|timeout|nice|ionice|nohup|setsid|stdbuf|env|command"
              r"|exec|eatmydata|sudo|doas)"),
    re.IGNORECASE)


def _runs_inert_consumer(owner_words: list[str]) -> bool:
    """Whether the simple command *owner_words* (quotes masked, heredoc operators removed) runs
    an allowlisted data consumer, after the same wrapper peeling as the lifecycle guard."""
    from cron.lifecycle_guard import _TRANSPARENT_COMMAND_PREFIXES, _peel_transparent_prefixes

    index = next((i for i, word in enumerate(owner_words) if not _ASSIGNMENT_WORD_RE.match(word)), None)
    if index is None:
        return False
    peeled = _peel_transparent_prefixes(owner_words, index)
    if peeled >= len(owner_words):
        return False
    for word in owner_words[index:peeled]:
        name = word.rsplit("/", 1)[-1]
        if word in _SHELL_STARTING_WRAPPER_OPTIONS or (
            name in _TRANSPARENT_COMMAND_PREFIXES and name not in _INERT_HEREDOC_WRAPPERS
        ):
            return False
    return bool(_INERT_HEREDOC_CONSUMER_RE.match(owner_words[peeled].rsplit("/", 1)[-1]))


def _span_end(command: str, cursor: int, closer: str) -> int:
    """Index just past the backslash-aware span opened at ``cursor``."""
    end = cursor + 1
    while end < len(command):
        if command[end] == closer:
            return end + 1
        end += 2 if command[end] == "\\" and end + 1 < len(command) else 1
    return end


def _mask_simple_quotes(command: str) -> str:
    """Blank inert quoted spans; keep ``$(``/backtick-bearing ones visible."""
    result = []
    cursor = 0
    while cursor < len(command):
        char = command[cursor]
        if char in "'\"":  # single quotes have no escapes; double quotes are backslash-aware
            end = (command.find("'", cursor + 1) + 1 if char == "'"
                   else _span_end(command, cursor, '"'))
            segment = command[cursor:end]
            if not segment.endswith(char):
                result.append(command[cursor:])
                break
            keep = char == '"' and ("$(" in segment or "`" in segment)
            result.append(segment if keep else char * 2)
            cursor = end
        elif char == "`":
            end = _span_end(command, cursor, "`")
            result.append(command[cursor:end])
            cursor = end
        else:
            result.append(char)
            cursor += 1
    return "".join(result)


def _parse_heredoc_operator(command: str, index: int):
    """Parse one ``<<`` opener -> ``(end_index, delimiter, strip_tabs, quoted)`` or None."""
    if not command.startswith("<<", index) or command.startswith("<<<", index):
        return None
    strip_tabs = command.startswith("-", index + 2)
    cursor = index + 3 if strip_tabs else index + 2
    while cursor < len(command) and command[cursor] in " \t":
        cursor += 1
    if cursor >= len(command) or command[cursor] in "\r\n":
        return None
    delimiter: list[str] = []
    quoted = False
    while cursor < len(command) and not (command[cursor].isspace() or command[cursor] in ";&|<>()"):
        char = command[cursor]
        if char == "\\":  # backslash-escaped char: quoted, literal
            if cursor + 1 >= len(command) or command[cursor + 1] in "\r\n":
                return None
            quoted = True
            delimiter.append(command[cursor + 1])
            cursor += 2
        elif char in "'\"":
            quoted = True
            cursor += 1
            while cursor < len(command) and command[cursor] != char:
                current = command[cursor]
                if current in "\r\n":
                    return None
                if char == '"' and current == "\\":
                    if cursor + 1 >= len(command):
                        return None
                    if command[cursor + 1] in '$`"\\\n':  # else backslash is literal in dquotes
                        cursor += 1
                        current = command[cursor]
                delimiter.append(current)
                cursor += 1
            if cursor >= len(command):  # unterminated quote
                return None
            cursor += 1
        else:
            delimiter.append(char)
            cursor += 1
    if not delimiter and not quoted:
        return None
    return cursor, "".join(delimiter), strip_tabs, quoted


def _is_fd_redirect_ampersand(command: str, index: int) -> bool:
    """Return whether ``&`` at ``index`` belongs to ``>&``/``<&``/``&>`` redirection."""
    before = command[index - 1] if index else ""
    after = command[index + 1] if index + 1 < len(command) else ""
    return before in "<>" or after == ">"


def _scan_heredoc_command_unit(command: str, start: int):
    """Scan one logical command.

    Return ``(end, specs, unknown_operator, post_heredoc_list_operator, owner_start)``.
    List operators before the first heredoc select the simple command that owns it. A list
    operator after a heredoc keeps the body visible because another command may consume it.
    """
    cursor = start
    quote = None
    comment = False
    specs = []
    unknown_operator = False
    post_heredoc_list_operator = False
    owner_start = start
    while cursor < len(command):
        char = command[cursor]
        if char == "\n" and (comment or quote is None):
            break
        # Backslash escapes (incl. line continuations) outside single quotes skip the next char.
        escaped = char == "\\" and quote != "'" and not comment and cursor + 1 < len(command)
        if comment or quote is not None or escaped:
            if char == quote:
                quote = None
            cursor += 2 if escaped else 1
        elif char in "'\"`":
            quote = char
            cursor += 1
        elif char == "#" and (cursor == start or command[cursor - 1].isspace()
                              or command[cursor - 1] in ";&|()"):
            comment = True
            cursor += 1
        elif command.startswith("<<<", cursor):
            cursor += 3
        elif command.startswith("<<", cursor):
            parsed = _parse_heredoc_operator(command, cursor)
            if parsed is None:
                unknown_operator = True
                cursor += 2
            else:
                cursor, delimiter, strip_tabs, quoted = parsed
                specs.append((delimiter, strip_tabs, quoted))
        else:
            if char in ";|&" and not (
                char == "&" and _is_fd_redirect_ampersand(command, cursor)
            ):
                if specs:
                    post_heredoc_list_operator = True
                else:
                    owner_start = cursor + 1
            cursor += 1
    return cursor, specs, unknown_operator, post_heredoc_list_operator, owner_start


def _find_heredoc_close(
        command: str, body_start: int, delimiter: str, strip_tabs: bool) -> tuple[int, int] | None:
    """Return ``(terminator_line_start, position_after_it)`` for the exact terminator line."""
    cursor = body_start
    while True:
        newline = command.find("\n", cursor)
        after = len(command) if newline == -1 else newline + 1
        line = command[cursor:after].removesuffix("\n").removesuffix("\r")
        candidate = line.lstrip("\t") if strip_tabs else line
        if candidate == delimiter:
            return cursor, after
        if newline == -1:
            return None
        cursor = after


def _owner_runs_inert_consumer(
        command: str, command_start: int, command_end: int, owner_start: int) -> bool:
    """Whether the heredoc opener line is a plain simple command (optionally the first command of
    a ``name() {`` function body) whose peeled command is an allowlisted data consumer."""
    header = _FUNCTION_HEADER_RE.match(command, command_start, command_end)
    opener_start = header.end() if header else command_start
    masked_opener = _mask_simple_quotes(command[opener_start:command_end])
    if any(marker in masked_opener for marker in ("$(", "`", "<(", ">(", "(", ")", "{", "}")):
        return False
    owner = command[max(owner_start, opener_start):command_end]
    return _runs_inert_consumer(_mask_simple_quotes(owner).split())


def strip_inert_heredoc_bodies(command: str, *, _depth: int = 0) -> str:
    """Mask heredoc bodies that are provably inert data (see module docstring). The body of a
    quoted-delimiter heredoc that is NOT masked (``ssh h 'bash -s' <<'R'``) stays visible, but
    data-consumer heredocs nested inside it are classified and masked the same way."""
    # Runs on every terminal call: skip the state machine when no '<<' exists; stop past the last.
    if "<<" not in command:
        return command
    if _CONSUMER_SHADOW_RE.search(command):
        return command
    last_opener_index = command.rfind("<<")
    replacements: list[tuple[int, int, str]] = []
    command_start = 0
    while command_start <= last_opener_index:
        (
            command_end,
            specs,
            unknown_operator,
            post_heredoc_list_operator,
            owner_start,
        ) = _scan_heredoc_command_unit(command, command_start)
        if unknown_operator:
            return command
        if not specs:
            if command_end >= len(command):
                break
            command_start = command_end + 1
            continue
        if command_end >= len(command):
            return command  # opener with no body line: unterminated — leave visible
        body_cursor = command_end + 1
        bodies: list[tuple[int, int, int]] = []
        for delimiter, strip_tabs, _quoted in specs:
            closed = _find_heredoc_close(command, body_cursor, delimiter, strip_tabs)
            if closed is None:
                return command  # unterminated
            bodies.append((body_cursor, *closed))
            body_cursor = closed[1]
        quoted = [spec_quoted for _delimiter, _strip_tabs, spec_quoted in specs]
        if (
            all(quoted)
            and not post_heredoc_list_operator
            and _owner_runs_inert_consumer(command, command_start, command_end, owner_start)
        ):
            replacements += [(start, end, "\n" * command.count("\n", start, end))
                             for start, _terminator, end in bodies]
        elif _depth < _MAX_NESTED_HEREDOC_DEPTH:
            # An unquoted outer body expands locally first (`$(…)` runs here), so only a quoted
            # one is literal text whose nested heredocs can be judged on their own.
            for (start, terminator, _end), body_quoted in zip(bodies, quoted):
                inner = command[start:terminator]
                if body_quoted and "<<" in inner:
                    masked = strip_inert_heredoc_bodies(inner, _depth=_depth + 1)
                    if masked != inner:
                        replacements.append((start, terminator, masked))
        command_start = body_cursor
    # Single-pass rebuild (replacements are sorted and non-overlapping).
    parts: list[str] = []
    previous = 0
    for start, end, replacement in replacements:
        parts += [command[previous:start], replacement]
        previous = end
    return "".join(parts) + command[previous:]
