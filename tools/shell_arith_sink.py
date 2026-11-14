"""Find a command substitution that sits inside a bash arithmetic context.

Bash evaluates arithmetic text recursively, and an array subscript in it runs command
substitutions, so a substitution's OUTPUT inside arithmetic is shell code wherever the
substitution sits: ``x=$(( 1 + $(echo 'a[$(bash F)]') ))`` runs ``bash F``
(t_01a1d6ae05c19594). Arithmetic contexts covered: ``$(( ))``, ``(( ))`` (``for ((``
too), ``$[ ]``, ``let``, ``[[ … -eq … ]]`` (any of the six integer operators), an array
subscript (``a[…]=``, ``a=([…]=value)``, ``${a[…]}``, and quoted variable names
passed to builtins such as ``unset``/``printf -v``/``test -v``), a ``${x:offset:length}``
substring, and any
``declare/local/typeset/readonly/export -i`` integer assignment (coarse: any substitution
then counts). ``[ … -eq … ]``/``test`` are not arithmetic and stay out.

The scan is a small linear tokenizer, not a parser. It handles shell quoting separately
from arithmetic quoting and keeps parameter-word delimiters local to their frame.
Uncertain quoting fails closed; lexical openers must still be recognized accurately
(e.g. ``$${`` is a PID followed by a literal brace, not a parameter expansion).
"""

from __future__ import annotations

import bisect
import re

from tools.shell_command_position import QuotedNameClassifier

# `$(date +%s)` prints only digits: the one producer whose output arithmetic may hold.
DATE_PRODUCER_BODY = r"""[ \t]*date[ \t]+(?:\+%s|'\+%s'|"\+%s")[ \t]*\)"""
_DATE_PRODUCER_RE = re.compile(DATE_PRODUCER_BODY)

_TOKENS = (r"\\.|'|\"|`|\$\$|\$\(\(|\$\(|\$\[|\$\{|\(\(|\(|\)|\[\[|\]\]|\[|\]|\}|#"
           r"|\$'|<<<|<<|\n|(?<![\w.$/-])let[ \t]+")
_TOKEN_RE = re.compile(_TOKENS, re.DOTALL)
_NO_HEREDOC_TOKEN_RE = re.compile(_TOKENS.replace(r"|\n", ""), re.DOTALL)
_PATTERN_TOKEN_RE = re.compile(_TOKENS + r"|/", re.DOTALL)
# Inside `let` the list operators end the arithmetic too; elsewhere skipping them keeps the scan
# of a long script fast.
_LET_TOKEN_RE = re.compile(_TOKENS + r"|[;&|\n]", re.DOTALL)
# Unquoted command frames also see list operators: each starts a new simple command,
# so a quoted builtin target is classified from its own command, not the frame start.
_SEPARATOR_TOKEN_RE = _LET_TOKEN_RE
_INT_OPERATOR_RE = re.compile(r"(?<![\w-])-(?:eq|ne|lt|le|gt|ge)(?![\w-])")
_INTEGER_DECLARATION_RE = re.compile(
    r"(?<![\w.-])(?:declare|typeset|local|readonly|export)(?:[ \t]+[-+][A-Za-z]+)*"
    r"[ \t]+-[A-Za-z]*i")
_PARAM_NAME_RE = re.compile(r"[#!]?(?:[A-Za-z_]\w*|\d+|[@*#?$!-])")
_COMMENT_PRECEDERS = frozenset(" \t\n;&|(")

# Characters that cannot end a variable name or an expansion: a quoted `[` after one
# (or after a quote concatenation ending in one) never names an array subscript.
_NOT_NAME_END = frozenset(" \t\n[],:=./+%^~")

# Frames are [kind, closer, depth, in_double_quotes]; command frames also carry the
# source start of their current simple command (so outer quotes cannot change nested
# command position) and that command's incremental quoted-word classifier.
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
    stack: list[list] = [[_CMD, None, 0, False, 0, None]]
    pos, end = 0, len(command)
    command_tokens = _TOKEN_RE if "<<" in command else _NO_HEREDOC_TOKEN_RE
    while pos < end:
        top = stack[-1]
        if top[1] == "let" or (top[0] == _CMD and not top[3]):
            tokens = _SEPARATOR_TOKEN_RE
        else:
            tokens = _PATTERN_TOKEN_RE if top[0] == _PARAM_PATTERN else command_tokens
        match = tokens.search(command, pos)
        if match is None:
            break
        token, start = match.group(), match.start()
        pos = match.end()
        top = stack[-1]
        kind, quoted = top[0], top[3]
        if token[0] == "\\":
            if _escaped_opener(command, token, start) and _subscript_word_before(command, stack, pos):
                return True
            continue
        if token in ("`", "$(", "$((", "$[", "${"):
            pos, sink = _expansion_token(command, token, pos, stack, date_exempt)
            if sink:
                return True
            backtick_open ^= token == "`"
            continue
        if token == '"':
            if quoted and command[start - 1] == "$" and _subscript_word_before(command, stack, pos):
                return True  # `"$"(…)`: the `$` joins a later `(` after quote removal
            top[3] = not quoted
            continue
        skipped = _skip_data(command, token, start, pos, quoted, stack, pending,
                             date_exempt, _depth)
        if skipped is not None:
            if skipped < 0:
                return True
            if token == "\n" and kind == _CMD:
                top[4] = skipped  # heredoc bodies end their command line too
            pos = skipped
            continue

        if token == "[" and quoted and kind == _CMD and _quoted_subscript_sink(command, start, top):
            stack.append([_ARITH, "]", 0, False])
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


def _quoted_subscript_sink(command: str, start: int, top: list) -> bool:
    """Whether the double-quoted ``[`` at *start* opens a builtin's array subscript.

    Linear overall: only a ``[`` that can follow a name is classified, by one
    incremental classifier per simple command (see ``QuotedNameClassifier``).
    A quoted element of a compound array literal, ``a=("x[…]")``, is a plain value.
    """
    if top[1] == "array":
        return False
    index = start - 1
    while index >= top[4] and command[index] in "\"'":
        index -= 1
    if index < top[4] or command[index] in _NOT_NAME_END:
        return False
    if top[5] is None or top[5].base != top[4]:
        top[5] = QuotedNameClassifier(top[4])
    return top[5].classify(command, start)


def _compound_assignment_before(command: str, start: int) -> bool:
    """Whether ``name=`` or ``name+=`` (a word of its own) ends just before *start*."""
    index = start - 1
    if index < 0 or command[index] != "=":
        return False
    index -= 1
    if index >= 0 and command[index] == "+":
        index -= 1
    name_end = index
    while index >= 0 and (command[index].isalnum() or command[index] == "_"):
        index -= 1
    return index < name_end and (index < 0 or command[index] in " \t\n\r\f\v;({")


def _quick_result(command: str, depth: int) -> bool | None:
    if depth >= 32:
        return True
    # Literal heredoc bodies can be prose, even for unknown consumers. Without
    # expansion introducers no substitution can form, regardless of quoting.
    # Never apply this exception to top-level shell text or code-like bodies.
    if depth and not any(mark in command for mark in ("$", "`", "<(", ">(")):
        return False
    if "$(" not in command and not any(mark in command for mark in ("'", '"', "`", "\\")):
        return False
    if ("$(" in command or "`" in command) and _INTEGER_DECLARATION_RE.search(command):
        return True
    return None


def _arithmetic_quotes(stack) -> bool:
    """Parameter words inherit the nearest evaluator's lexical quote rules."""
    for frame in reversed(stack):
        kind, closer = frame[0], frame[1]
        if kind == _CMD:
            return False
        if kind == _ARITH:
            # `let` and [[ ]] parse shell words before evaluating operands.
            return closer not in ("let", "]]")
    return False


def _word_arithmetic_frame(stack) -> list | None:
    """The ``let`` / ``[[ … -eq … ]]`` frame whose operands are evaluated after quote removal.

    Bash removes the shell quotes of those operands first, then evaluates the remaining
    text as arithmetic, and an array subscript in that text runs its command substitutions:
    ``let 'a[$(bash F)]=1'`` and ``[[ 1 -eq 'a[$(bash F)]' ]]`` run ``bash F``. Subscript
    (``]``) and parameter frames inside such an operand belong to the same word.
    """
    for frame in reversed(stack):
        if frame[0] == _CMD:
            return None
        if frame[0] == _ARITH and frame[1] != "]":
            return frame if frame[1] in ("let", "]]") else None
    return None


def _subscript_word_before(command: str, stack, pos: int) -> bool:
    """Whether a quoted/escaped substitution opener ending before *pos* sits in a word
    arithmetic frame after a ``[`` (coarse: any ``[`` in the frame so far, so separate
    ``let`` arguments or ``[[`` operands also count; that only fails closed)."""
    frame = _word_arithmetic_frame(stack)
    if frame is None:
        return False
    if command.find("[", frame[4], pos) != -1:
        return True
    frame[4] = pos  # no `[` up to here: later queries resume past it (linear overall)
    return False


def _escaped_opener(command: str, token: str, start: int) -> bool:
    """Whether a backslash escape can form ``$(`` or a backtick once quotes are removed."""
    if token == "\\`":
        return True
    if token == "\\$":
        return command[start + 2:start + 3] in ("(", "\\", "'", '"')
    if token == "\\(":
        index = start - 1
        while index >= 0 and command[index] in "'\"":
            index -= 1
        return index >= 0 and command[index] == "$"
    return False


def _quoted_opener(span: str, date_exempt: bool) -> bool:
    """Whether a quoted span (closing quote included) can supply a substitution opener
    after quote removal: ``'$('`` itself, or a trailing ``$`` that meets a later ``(``."""
    return _span_substitutes(span, date_exempt) or span[:-1].endswith("$")


def _span_substitutes(span: str, date_exempt: bool) -> bool:
    """Whether an apostrophe span evaluated as arithmetic holds a command substitution."""
    if "`" in span:
        return True
    return any(not (date_exempt and _DATE_PRODUCER_RE.match(span, m.end()))
               for m in re.finditer(r"\$\(", span))  # `$((` too: it may be `$( (…)…)`


def _skip_data(command, token, start, pos, quoted, stack, pending, date_exempt, depth) -> int | None:
    """Skip literal spans; None means a normal token, -1 means fail closed."""
    kind = stack[-1][0]
    if token in ("'", "$'"):
        if quoted:
            return pos
        close = _quote_close(command, pos, ansi=token == "$'")
        if close is None:
            return -1
        span = command[pos:close]
        if _arithmetic_quotes(stack) and _span_substitutes(span, date_exempt):
            # Bash still uses the apostrophes to find the frame's closer, but then
            # evaluates the text with double-quote semantics, so `$(`/backticks run.
            return -1
        if _word_arithmetic_frame(stack) is not None and (
                (token == "$'" and "\\" in span)  # escapes can spell both `[` and `$(`
                or (_quoted_opener(span, date_exempt)
                    and _subscript_word_before(command, stack, close))):
            return -1
        return close
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


def _expansion_token(command, token, pos, stack, date_exempt) -> tuple[int, bool]:
    """Open an expansion frame, or flag a producer whose output is arithmetic."""
    if token == "`":
        if stack[-1][0] in (_ARITH, _PARAM_ARITH):
            return pos, True
        if stack[-1][1] == "`":
            stack.pop()
        else:
            stack.append([_CMD, "`", 0, False, pos, None])
        return pos, False
    if token == "$(":
        if stack[-1][0] in (_ARITH, _PARAM_ARITH) and not (
                date_exempt and _DATE_PRODUCER_RE.match(command, pos)):
            return pos, True
        stack.append([_CMD, ")", 0, False, pos, None])
    elif token == "${":
        pos = _open_parameter(command, pos, stack)
    else:  # $(( or $[
        stack.append([_ARITH, "))" if token == "$((" else "]", 0, False])
    return pos, False


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
        stack.append([_ARITH, "let", 0, False, pos])  # [4]: see _subscript_word_before
        return pos
    if token == "((":
        stack.append([_ARITH, "))", 0, False])
        return pos
    if token in (";", "&", "|", "\n"):
        # `>&`, `<&`, `&>` and `>|` are redirections, not list operators.
        if not ((token in "&|" and command[start - 1:start] in ("<", ">"))
                or (token == "&" and command[pos:pos + 1] == ">")):
            stack[-1][4] = pos
        return pos
    if token == "(":
        closer = "array" if _compound_assignment_before(command, start) else ")"
        stack.append([_CMD, closer, 0, False, pos, None])
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
            stack.append([_ARITH, "]]", 0, False, pos])
        return pos
    if token == "[" and (stack[-1][1] == "array" or (
            start > 0 and (command[start - 1].isalnum() or command[start - 1] == "_"))):
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
