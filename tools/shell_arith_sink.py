"""Find a ``$(…)`` substitution that sits inside a bash arithmetic context.

Bash evaluates arithmetic text recursively, and an array subscript in it runs command
substitutions, so a substitution's OUTPUT inside arithmetic is shell code wherever the
substitution sits: ``x=$(( 1 + $(echo 'a[$(bash F)]') ))`` runs ``bash F``
(t_01a1d6ae05c19594). Arithmetic contexts covered: ``$(( ))``, ``(( ))`` (``for ((``
too), ``$[ ]``, ``let``, ``[[ … -eq … ]]`` (any of the six integer operators), an array
subscript (``a[…]=``, ``${a[…]}``), a ``${x:offset:length}`` substring, and any
``declare/local/typeset/readonly/export -i`` integer assignment (coarse: any substitution
then counts). ``[ … -eq … ]``/``test`` are not arithmetic and stay out.

The scan is a small linear tokenizer, not a parser: quotes and comments are honoured, and
whatever it cannot match (an unclosed frame, a stray paren) only widens the arithmetic
region, so a mistake fails closed (a false sink only makes the lifecycle walk scan more).
"""

from __future__ import annotations

import bisect
import re

# `$(date +%s)` prints only digits: the one producer whose output arithmetic may hold.
DATE_PRODUCER_BODY = r"""[ \t]*date[ \t]+(?:\+%s|'\+%s'|"\+%s")[ \t]*\)"""
_DATE_PRODUCER_RE = re.compile(DATE_PRODUCER_BODY)

_TOKENS = (r"\\.|'|\"|`|\$\(\(|\$\(|\$\[|\$\{|\(\(|\(|\)|\[\[|\]\]|\[|\]|\}|#"
           r"|\$'|<<<|<<|\n|(?<![\w.$/-])let[ \t]+")
_TOKEN_RE = re.compile(_TOKENS, re.DOTALL)
_NO_HEREDOC_TOKEN_RE = re.compile(_TOKENS.replace(r"|\n", ""), re.DOTALL)
_PATTERN_TOKEN_RE = re.compile(_TOKENS + r"|/", re.DOTALL)
# Inside `let` the list operators end the arithmetic too; elsewhere skipping them keeps the scan
# of a long script fast.
_LET_TOKEN_RE = re.compile(_TOKENS + r"|[;&|\n]", re.DOTALL)
_INT_OPERATOR_RE = re.compile(r"(?<![\w-])-(?:eq|ne|lt|le|gt|ge)(?![\w-])")
_INTEGER_DECLARATION_RE = re.compile(
    r"(?<![\w.-])(?:declare|typeset|local|readonly|export)(?:[ \t]+[-+][A-Za-z]+)*"
    r"[ \t]+-[A-Za-z]*i")
_PARAM_NAME_RE = re.compile(r"[#!]?(?:[A-Za-z_]\w*|\d+|[@*#?$!-])")
_COMMENT_PRECEDERS = frozenset(" \t\n;&|(")

# Frame kinds. A frame is [kind, closer, depth, in_double_quotes].
_CMD, _ARITH, _PARAM = "cmd", "arith", "param"
_PARAM_ARITH = "param_arith"  # parameter word feeding an arithmetic evaluator
_PARAM_PATTERN = "param_pattern"  # only its replacement feeds the evaluator


def substitution_in_arithmetic(command: str, *, date_exempt: bool, _depth: int = 0) -> bool:
    """Whether *command* holds a ``$(`` (not the exempt ``$(date +%s)`` when *date_exempt*)
    whose nearest enclosing context is arithmetic.

    Uncertain quoting and expanding heredoc bodies deliberately fail closed,
    even for benign producers. Unmasked literal bodies are scanned as programs;
    only the caller may mask a proven-inert body.
    """
    quick = _quick_result(command, _depth)
    if quick is not None:
        return quick
    closes = [m.start() for m in re.finditer(r"\]\]", command)]
    operators = [m.start() for m in _INT_OPERATOR_RE.finditer(command)]
    pending = []
    backtick_open = False
    stack: list[list] = [[_CMD, None, 0, False]]
    pos, end = 0, len(command)
    command_tokens = _TOKEN_RE if "<<" in command else _NO_HEREDOC_TOKEN_RE
    while pos < end:
        tokens = _LET_TOKEN_RE if stack[-1][1] == "let" else command_tokens
        tokens = {_PARAM_PATTERN: _PATTERN_TOKEN_RE}.get(stack[-1][0], tokens)
        match = tokens.search(command, pos)
        if match is None:
            break
        token, start = match.group(), match.start()
        pos = match.end()
        top = stack[-1]
        kind, quoted = top[0], top[3]
        if token == "`":
            backtick_open = not backtick_open
            continue
        if token[0] == "\\":
            continue
        if token == '"':
            top[3] = not quoted
            continue
        skipped = _skip_data(command, token, start, pos, quoted, kind, pending,
                             date_exempt, _depth)
        if skipped is not None:
            if skipped < 0:
                return True
            pos = skipped
            continue
        if token == "$((":
            stack.append([_ARITH, "))", 0, False])
            continue
        if token == "$(":
            if kind in (_ARITH, _PARAM_ARITH) and not (
                    date_exempt and _DATE_PRODUCER_RE.match(command, pos)):
                return True
            stack.append([_CMD, ")", 0, False])
            continue
        if token == "$[":
            stack.append([_ARITH, "]", 0, False])
            continue
        if token == "${":
            pos = _open_parameter(command, pos, stack)
            continue
        if quoted:
            continue  # inside "…" only expansions (handled above) matter
        if kind == _CMD:
            pos = _cmd_token(command, token, start, pos, stack, closes, operators)
        elif kind == _ARITH:
            pos = _arith_token(command, token, pos, stack)
        else:
            pos = _parameter_token(token, start, pos, stack)
    # Callers also pass opener-only command units; queued bodies are not quote errors.
    return backtick_open or any(frame[3] for frame in stack)


def _quick_result(command: str, depth: int) -> bool | None:
    if depth >= 32:
        return True
    # Literal heredoc bodies can be prose, even for unknown consumers. Without
    # expansion introducers no substitution can form, regardless of quoting.
    # Never apply this exception to top-level shell text or code-like bodies.
    if depth and not any(mark in command for mark in ("$", "`", "<(", ">(")):
        return False
    if "$(" not in command and not any(mark in command for mark in ("'", '"', "`")):
        return False
    if "$(" in command and _INTEGER_DECLARATION_RE.search(command):
        return True
    return None


def _skip_data(command, token, start, pos, quoted, kind, pending, date_exempt, depth) -> int | None:
    """Skip literal spans; None means a normal token, -1 means fail closed."""
    if token in ("'", "$'"):
        return pos if quoted else _quote_close(command, pos, ansi=token == "$'") or -1
    if quoted:
        return None
    # Lazy import: shell_heredoc calls this scanner, but owns delimiter parsing.
    from tools.shell_heredoc import (
        _find_heredoc_close, _owner_runs_inert_consumer, _parse_heredoc_operator,
        _scan_heredoc_command_unit,
    )

    if kind == _CMD and token == "<<":
        parsed = _parse_heredoc_operator(command, start)
        if parsed is None:
            return -1
        pos, delimiter, strip_tabs, literal = parsed
        line_start = command.rfind("\n", 0, start) + 1
        unit_end, _, unknown, post_list, owner = _scan_heredoc_command_unit(command, line_start)
        inert = not unknown and not post_list and _owner_runs_inert_consumer(
            command, line_start, unit_end, owner)
        pending.append((delimiter, strip_tabs, literal, inert))
        return pos
    if token != "\n" or not pending:
        return None
    for delimiter, strip_tabs, literal, inert in pending:
        close = _find_heredoc_close(command, pos, delimiter, strip_tabs)
        if close is None:
            # Opener-only caller views may omit bodies. Scan the remainder rather
            # than hiding later sinks or unmatched quotes behind an unknown body.
            break
        body_end, after = close
        # Unquoted bodies expand; quoted bodies may be programs for bash/ssh or
        # an unknown consumer. Only the caller's proven-inert masking may hide
        # their substitutions. In either case apostrophes are not outer quotes.
        body = command[pos:body_end]
        if literal and (not inert or "$(" in body or "`" in body):
            if substitution_in_arithmetic(body, date_exempt=date_exempt, _depth=depth + 1):
                return -1
        elif not literal and ("$(" in body or "`" in body):
            return -1
        pos = after
    pending.clear()
    return pos


def _quote_close(command: str, pos: int, *, ansi: bool) -> int | None:
    """Return past a single quote, honouring escapes only in ANSI-C strings."""
    while pos < len(command):
        if command[pos] == "'":
            return pos + 1
        pos += 2 if ansi and command[pos] == "\\" else 1
    return None



def _parameter_token(token, start, pos, stack) -> int:
    if stack[-1][0] == _PARAM_PATTERN and token == "/":
        stack[-1][0] = _PARAM_ARITH
    elif token == "}":
        stack.pop()
    # Parentheses and brackets are literal parameter-word text, not closers
    # for enclosing arithmetic. Only } ends this lexical frame.
    return pos


def _open_parameter(command: str, pos: int, stack: list[list]) -> int:
    """Push the frame for a ``${`` opened before *pos*; return where scanning resumes."""
    name = _PARAM_NAME_RE.match(command, pos)
    after = name.end() if name else pos
    stack.append([_PARAM, "}", 0, False])
    return _after_parameter_name(command, after, stack)


def _after_parameter_name(command: str, pos: int, stack: list[list]) -> int:
    """At the end of a ``${name`` (or of its ``[subscript]``): a subscript or a substring
    offset is arithmetic."""
    following = command[pos:pos + 2]
    if following[:1] == "[":
        stack.append([_ARITH, "]", 0, False])
        return pos + 1
    if following[:1] == ":" and following[1:2] not in ("-", "=", "+", "?"):
        stack[-1] = [_ARITH, "}", 0, False]
        return pos + 1
    # Classify after the subscript too. Keep parameter lexical rules: literal
    # brackets in words and patterns must not open arithmetic frames.
    if stack[-2][0] in (_ARITH, _PARAM_ARITH):
        operator = following.lstrip(":")[:1]
        if operator in ("-", "=", "+"):
            stack[-1][0] = _PARAM_ARITH
        elif following.startswith("/"):
            stack[-1][0] = _PARAM_PATTERN
            return pos + (2 if following == "//" else 1)
    return pos


def _cmd_token(command, token, start, pos, stack, closes, operators) -> int:
    if token == "#":
        if start == 0 or command[start - 1] in _COMMENT_PRECEDERS:
            newline = command.find("\n", pos)
            return len(command) if newline == -1 else newline
        return pos
    if token.startswith("let"):
        stack.append([_ARITH, "let", 0, False])
        return pos
    if token == "((":
        stack.append([_ARITH, "))", 0, False])
        return pos
    if token == "(":
        stack.append([_CMD, ")", 0, False])
        return pos
    if token == ")":
        if len(stack) > 1:
            stack.pop()
        return pos
    if token == "[[":
        index = bisect.bisect_left(closes, pos)
        close = closes[index] if index < len(closes) else len(command)
        operator = bisect.bisect_left(operators, pos)
        if operator < len(operators) and operators[operator] < close:
            stack.append([_ARITH, "]]", 0, False])
        return pos
    if token == "[" and start > 0 and (command[start - 1].isalnum() or command[start - 1] == "_"):
        stack.append([_ARITH, "]", 0, False])  # `a[…]=` subscript
    return pos


def _arith_token(command, token, pos, stack) -> int:
    top = stack[-1]
    closer = top[1]
    if closer == "let" and (token in (";", "&", "|", "\n") or (token == ")" and not top[2])):
        stack.pop()
        return pos - len(token)  # the list operator / paren belongs to the outer frame
    if token in ("(", "(("):
        top[2] += len(token)
        return pos
    if token == ")":
        if top[2]:
            top[2] -= 1
        elif closer in ("))", ")"):
            stack.pop()
            if closer == "))" and command.startswith(")", pos):
                pos += 1
        return pos
    if token in ("[", "[["):
        for _ in token:
            stack.append([_ARITH, "]", 0, False])
        return pos
    if token in ("]", "]]"):
        if token == "]]" and closer == "]]":
            stack.pop()
            return pos
        for _ in token:
            if stack[-1][0] != _ARITH or stack[-1][1] != "]":
                break
            stack.pop()
            if stack[-1][0] == _PARAM:
                pos = _after_parameter_name(command, pos, stack)
        return pos
    if token == "}" and closer == "}":
        stack.pop()
    return pos
