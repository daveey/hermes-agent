"""What bash's ``printf`` builtin and ``echo -e`` print, for the lifecycle guard's output-sink walk.

Under an output sink (``printf FORMAT ARGS | bash``) the guard walks a producer's output as an
executed command, so the walk needs the text bash really prints. Taking every conversion's
argument whole is not enough, because these printf features change which words come out:

- ``*`` as the width or precision takes an argument (``printf '%*s %s' 5 bash X`` prints
  ``    bash X``; the whole-argument walk read ``5 bash`` and lost X).
- A precision truncates ``%s``/``%b`` (``printf '%.4s %s' bashrc X`` prints ``bash X``).
- ``%b`` decodes ``\\xHH``, ``\\0NNN`` and friends in its argument and stops all output at
  ``\\c`` (``printf '%b %s' 'ba\\x73h' X`` prints ``bash X``); the format decodes its own escapes
  too (``printf 'ba\\x73h %s' X``).
- Numbers print as C prints them (``printf 'bash D/%#x.sh' 0`` prints ``bash D/0.sh``): see
  ``lifecycle_guard_printf_number``.
- ``-v NAME`` takes an operand, so the format is the word after NAME (printed_argument_lines).

The model follows bash 5's ``printf.def``: flags ``#'-+ 0``, a width and a precision (digits or
``*``), the ignored length modifiers ``hjlLtz``, then one of ``csbqQdiouxXeEfFgGaA``, ``%(FMT)T``
or ``%%``. An invalid conversion stops the output there, as in bash. Output past the guard's scan
budget is cut short (the guard refuses it as oversized anyway). Callers also walk the arguments
without the format and the earlier whole-argument substitution, so the model is one variant
among several and never the only text scanned. Unicode escapes assume UTF-8; in a C locale bash
may retain non-ASCII Unicode escapes verbatim. Empty ``%c`` is modelled without bash's NUL,
which bash drops again when reading script input.
"""

import re
import time
from typing import Callable, Iterator, Optional, cast

from cron.lifecycle_guard_printf_number import (
    FLOAT_MODELS, X87, FloatModel, decimal_digits, format_number, integer)

PRINTF_MAX_ROUNDS = 64
# The guard's whole-walk scan budget (lifecycle_guard._MAX_LIFECYCLE_SCAN_BYTES, pinned equal by a
# test; importing it here would be circular). Output past it is refused as oversized either way.
PRINTF_MAX_OUTPUT = 1 << 20
_MAX_FIELD = 4096  # width and numeric precision cap: `printf '%999999999s'` cannot eat memory

_OCTAL_DIGITS = "01234567"
_HEX_DIGITS = "0123456789abcdefABCDEF"
_SIMPLE_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
    "v": "\v", "\\": "\\",
}
# Decoded in a printf format but kept as written by `%b` and `echo -e`.
_FORMAT_ONLY_ESCAPES = {"'": "'", '"': '"', "?": "?"}
# flags, width, precision, length modifiers, then the conversion (or `(FMT)T`).
_SPEC_RE = re.compile(r"([#'\-+ 0]*)(\*|[0-9]*)(?:\.(\*|[0-9]*))?[hjlLtz]*(?:\(([^)]*)\)(T)|(.))?",
                      re.DOTALL)
_STRING_CONVERSIONS = frozenset("csbqQT")
_INTEGER_CONVERSIONS = frozenset("diouxX")
_FLOAT_CONVERSIONS = frozenset("eEfFgGaA")
_FLOAT_CONVERSION_RE = re.compile(r"%[#'\-+ 0-9.*hjlLtz]*[eEfFgGaA]")
_CONVERSIONS = _STRING_CONVERSIONS | _INTEGER_CONVERSIONS | _FLOAT_CONVERSIONS
_SAFE_CHAR_RE = re.compile(r"[A-Za-z0-9_@%+=:,./-]")


def _decode_escape(text: str, index: int, *, echo_style: bool) -> tuple[str, int, bool]:
    """Decode the backslash escape at ``text[index]``: ``(output, next index, stop)``.

    ``echo_style`` selects ``%b``/``echo -e`` rules (``\\0NNN`` octal, ``\\c`` stops all
    output, ``\\'`` kept) over the format's (``\\NNN`` octal, ``\\c`` kept as written)."""
    if index + 1 >= len(text):
        return "\\", index + 1, False
    char = text[index + 1]
    if char in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[char], index + 2, False
    if char in _FORMAT_ONLY_ESCAPES and not echo_style:
        return _FORMAT_ONLY_ESCAPES[char], index + 2, False
    if char == "c" and echo_style:
        return "", index + 2, True
    if char in _OCTAL_DIGITS:
        start = index + 2 if echo_style and char == "0" else index + 1
        end = start
        while end < len(text) and end < start + 3 and text[end] in _OCTAL_DIGITS:
            end += 1
        return chr(int(text[start:end] or "0", 8) & 0xFF), end, False
    digits = {"x": 2, "u": 4, "U": 8}.get(char)
    if digits:
        start = end = index + 2
        while end < len(text) and end < start + digits and text[end] in _HEX_DIGITS:
            end += 1
        value = int(text[start:end], 16) if end > start else None
        if value is not None and (char == "x" or value < 0x110000):
            return chr(value), end, False
    # In a format an unknown escape consumes only the backslash: the next `%`
    # still starts a conversion. Consuming it would shift every subsequent argument.
    if not echo_style:
        return "\\", index + 1, False
    return "\\" + char, index + 2, False


def decode_c_escapes(text: str, *, echo_style: bool) -> str:
    """*text* with its backslash escapes decoded as a printf format (``echo_style`` False) or as
    a ``%b`` argument / ``echo -e`` word (True, which also drops everything from ``\\c`` on)."""
    return _decode(text, echo_style=echo_style)[0]


def _decode(text: str, *, echo_style: bool) -> tuple[str, bool]:
    out: list[str] = []
    index = 0
    while index < len(text):
        backslash = text.find("\\", index)
        if backslash < 0:
            out.append(text[index:])
            break
        out.append(text[index:backslash])
        decoded, index, stop = _decode_escape(text, backslash, echo_style=echo_style)
        out.append(decoded)
        if stop:
            return "".join(out), True
    return "".join(out), False


def _pad(text: str, flags: str, width: int) -> str:
    return text.ljust(width) if "-" in flags else text.rjust(width)


def _quoted(text: str) -> str:
    """``%q``: bash backslash-quotes each special character, so the shell reading it gets *text*
    back."""
    return "".join(c if _SAFE_CHAR_RE.match(c) else "\\" + c for c in text) or "''"


def _decoded(text: str, precision: Optional[int], _time_format: str
             ) -> tuple[str, Optional[int], bool]:
    """``%b``: the argument's escapes decoded as ``echo -e`` does; ``\\c`` ends all output."""
    text, stop = _decode(text, echo_style=True)
    return text, precision, stop


def _strftime(text: str, time_format: str) -> str:
    seconds = integer(text) if text else -1
    try:
        return time.strftime(time_format, time.localtime(None if seconds < 0 else seconds))
    except (ValueError, OverflowError, OSError):
        return ""


# Each string conversion: (argument, precision, %(FMT)T format) -> (text, precision, stop).
# The returned precision then truncates the text (None: no truncation).
_STRING_HANDLERS: dict[str, Callable[[str, Optional[int], str],
                                     tuple[str, Optional[int], bool]]] = {
    "s": lambda text, precision, _fmt: (text, precision, False),
    "c": lambda text, _precision, _fmt: (text[:1], None, False),
    "b": _decoded,
    "q": lambda text, precision, _fmt: (_quoted(text), precision, False),
    # `%Q` truncates the argument before quoting, so the quoting survives.
    "Q": lambda text, precision, _fmt: (
        _quoted(text if precision is None else text[:precision]), None, False),
    "T": lambda text, precision, fmt: (_strftime(text, fmt), precision, False),
}


def _format_string(conversion: str, flags: str, width: int, precision: Optional[int],
                   argument: Optional[str], time_format: str) -> tuple[str, bool]:
    text, precision, stop = _STRING_HANDLERS[conversion](argument or "", precision, time_format)
    if precision is not None:
        text = text[:precision]
    return _pad(text, flags, width), stop


def printf_output(format_text: str, arguments: list[str], model: FloatModel = X87) -> str:
    """What ``printf FORMAT ARGUMENTS`` prints in bash: conversions per the module docstring, the
    format's escapes decoded, and the format repeated while arguments remain and the last round
    consumed one (at most ``PRINTF_MAX_ROUNDS`` rounds). Missing arguments print as empty or 0.
    Floats print under *model*, the host's ``long double``."""
    out: list[str] = []
    position = 0
    size = 0  # characters in `out` (counted at each conversion and each round's end)

    def take() -> Optional[str]:
        nonlocal position
        if position >= len(arguments):
            return None
        position += 1
        return arguments[position - 1]

    def star(value: str) -> Optional[int]:
        """A width or precision: digits, or `*` taking the next argument as a number."""
        return integer(take() or "") if value == "*" else (decimal_digits(value) if value else None)

    for _round in range(PRINTF_MAX_ROUNDS):
        start = position
        index = 0
        while index < len(format_text):
            char = format_text[index]
            if char == "\\":
                decoded, index, _stop = _decode_escape(format_text, index, echo_style=False)
                out.append(decoded)
                continue
            if char != "%":
                out.append(char)
                index += 1
                continue
            if format_text[index + 1 : index + 2] == "%":
                out.append("%")
                index += 2
                continue
            # Every group is optional, so the spec always matches (possibly empty).
            match = cast(re.Match[str], _SPEC_RE.match(format_text, index + 1))
            flags, width_text, precision_text, time_format, time_conversion, conversion = (
                match.groups())
            conversion = time_conversion or conversion
            if conversion not in _CONVERSIONS:
                return "".join(out)  # bash: invalid or missing format character ends the output
            width = star(width_text) or 0
            if width < 0:
                flags, width = flags + "-", -width
            width = min(width, _MAX_FIELD)  # past the cap a width only adds more blanks
            precision = None if precision_text is None else (star(precision_text) or 0)
            if precision is not None and precision < 0:
                precision = None
            argument = take()
            if conversion in _STRING_CONVERSIONS:
                piece, stop = _format_string(conversion, flags, width, precision, argument,
                                             time_format or "")
            else:
                piece, stop = format_number(conversion, flags, width, precision, argument or "",
                                            max_field=_MAX_FIELD, model=model), False
            out.append(piece)
            size += len(piece)
            # `%b`'s `\c` ends all output. Past PRINTF_MAX_OUTPUT the text already exceeds the
            # guard's whole-walk scan budget, which refuses it: building more only costs memory.
            if stop or size > PRINTF_MAX_OUTPUT:
                return "".join(out)
            index = match.end()
        size = sum(map(len, out))
        if position >= len(arguments) or position == start or size > PRINTF_MAX_OUTPUT:
            break
    return "".join(out)


# C-style escapes a producer turns into whitespace (`printf 'bash /x/a.sh\n'`, `print('…\n')`):
# left in, `\n` would glue onto the path and hide the script it names.
_PRINTED_ESCAPE_RE = re.compile(r"\\[ntrvf]")
# bash's builtin echo options (`echo -n`, `echo -ne`): they are not printed, so left in the line
# the walk would read `-n` as the executed command and the script path as its argument.
_ECHO_OPTION_RE = re.compile(r"-[neE]+")


def printed_argument_lines(name: str, arguments: list[str]) -> Iterator[str]:
    """The line(s) an ``echo``/``printf`` with *arguments* may print: as written, without
    ``echo``'s leading option words, and (fail closed) without every leading ``-word``."""
    yield " ".join(arguments)
    if name == "echo":
        index = 0
        while index < len(arguments) and _ECHO_OPTION_RE.fullmatch(arguments[index]):
            index += 1
        yield " ".join(arguments[index:])
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 1
    yield " ".join(arguments[index:])
    if name != "printf":
        return
    # The format is not printed as written (`printf '%s %s' bash /x/a.sh` prints
    # `bash /x/a.sh`): also yield the arguments without it, and what bash prints for them
    # (`*` widths take an argument, precision truncates, `%b` decodes; see printf_output).
    # The format follows the options, where `-v NAME` takes an operand (`printf -v x FMT …`
    # stores the output in `$x`, which a later `echo $x | bash` runs).
    start = _printf_format_index(arguments)
    if start < len(arguments):
        yield " ".join(arguments[start + 1 :])
        # Under each platform's long double (the host's is one of them); a format without a
        # floating conversion prints the same under all, and the caller dedupes the lines.
        models = FLOAT_MODELS if _FLOAT_CONVERSION_RE.search(arguments[start]) else (X87,)
        for model in models:
            try:
                yield printf_output(arguments[start], arguments[start + 1 :], model)
            except (ValueError, ArithmeticError):
                # Fail closed: a model bug must not abort the guard's walk (its caller falls
                # back to the direct scan); the variants around this one are still walked.
                continue
    # Fail closed: also the earlier whole-argument walk (each conversion takes its argument as
    # written, format after every leading `-word`), so the model never yields less than it did.
    if index < len(arguments):
        yield " ".join(arguments[index + 1 :])
        yield _whole_argument_output(arguments[index], arguments[index + 1 :])


# One printf conversion (flags, width, precision, conversion letter) or a literal `%%`.
_WHOLE_CONVERSION_RE = re.compile(r"%(%|[-+ #0]*[0-9*]*(?:\.[0-9*]*)?[a-zA-Z])")


def _whole_argument_output(format_text: str, arguments: list[str]) -> str:
    """*format_text* with each conversion replaced by the next argument as written, repeated
    while arguments remain: only a walk variant beside printf_output."""
    remaining = list(arguments)
    rounds: list[str] = []

    def convert(match: re.Match[str]) -> str:
        if match[1] == "%":
            return "%"
        return remaining.pop(0) if remaining else ""

    while len(rounds) < PRINTF_MAX_ROUNDS:
        before = len(remaining)
        rounds.append(_WHOLE_CONVERSION_RE.sub(convert, format_text))
        if not remaining or len(remaining) == before:
            break
    return "".join(rounds)


def _printf_format_index(arguments: list[str]) -> int:
    """Index of the format in bash ``printf`` *arguments*: past ``-v NAME`` (or ``-vNAME``) and a
    ``--`` that ends the options."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return index + 1
        if argument == "-v":
            index += 2
        elif argument.startswith("-v"):
            index += 1
        else:
            break
    return index


def with_escapes_decoded(text: str) -> Iterator[str]:
    """*text*, and with its backslash escapes decoded the ways a producer may print them: C-style
    whitespace escapes as newlines (fail closed), as a printf format (`ba\\x73h`, `\\101`) and as
    `echo -e`/`%b` (`\\0163`, output ends at `\\c`). Python's `print('ba\\x73h …')` matches the
    printf decoding."""
    yield text
    if "\\" in text:
        yield _PRINTED_ESCAPE_RE.sub("\n", text)
        yield decode_c_escapes(text, echo_style=False)
        yield decode_c_escapes(text, echo_style=True)
