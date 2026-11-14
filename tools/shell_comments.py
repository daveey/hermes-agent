"""Conservative root-shell comment view for output-sink discovery.

Only full-line comments outside quotes/expansions/groups/heredocs are removed. Opaque
regions remain byte-for-byte visible to the sink scanner (including interpreter source).
Unmatched or unsupported lexical regions abandon masking, not sink discovery.
"""

from __future__ import annotations

import re

_OPEN = re.compile(r"\$\{|\$\(|\$\[|\$'|['\"`({\[]")
_HEREDOC = re.compile(r'''<<(-?)[ \t]*(?:'([^'\r\n]*)'|"([^"\\$`\r\n]*)")(?=$|[ \t\n;&|<>])''')

_CLOSE = {"${": "}", "$(": ")", "$[": "]", "$'": "'",
          "'": "'", '"': '"', "`": "`", "(": ")", "{": "}", "[": "]"}


def _opaque_end(text: str, pos: int, opener: str, depth: int = 0) -> int | None:
    """Skip a quoted/balanced region without changing any of its contents."""
    if depth > 32:
        return None
    closer = _CLOSE[opener]
    pos += len(opener)
    while pos < len(text):
        char = text[pos]
        if char == "\\" and opener != "'":
            pos += 2
            continue
        if char == closer:
            return pos + 1
        if opener in ("'", "$'", "`"):
            pos += 1
            continue
        if opener == '"':
            # Quotes inside substitutions belong to that substitution, not this string.
            nested = re.match(r"\$[({[]|`", text[pos:pos + 2])
        else:
            # Nested heredocs and shell comments can hide closing delimiters. Keeping the
            # entire original source is safer than trying to parse another shell grammar.
            if text.startswith("<<", pos) or (char == "#" and opener != "${"
                                              and text[pos - 1] in " \t\n;&|()"):
                return None
            nested = _OPEN.match(text, pos)
        if nested is not None:
            end = _opaque_end(text, pos, nested.group(), depth + 1)
            if end is None:
                return None
            pos = end
        else:
            pos += 1
    return None


def mask_root_comment_lines(command: str) -> str:
    """Mask proven comment lines, preserving offsets, newlines and all opaque source."""
    chars = list(command)
    pos, line_start, boundary = 0, True, True
    heredocs = []
    while pos < len(command):
        char = command[pos]
        if char == "\n":
            pos += 1
            for delimiter, strip_tabs in heredocs:
                terminator = re.compile(
                    r"^" + (r"\t*" if strip_tabs else "") + re.escape(delimiter) + r"(?:\n|$)",
                    re.MULTILINE,
                )
                closed = terminator.search(command, pos)
                if closed is None:
                    return command
                pos = closed.end()
            heredocs.clear()
            line_start = boundary = True
            continue
        if line_start and char in " \t":
            pos += 1
            continue
        if line_start and char == "#":
            end = command.find("\n", pos)
            end = len(command) if end == -1 else end
            chars[pos:end] = " " * (end - pos)
            pos = end
            continue
        line_start = False
        if char == "\\":
            if command[pos:pos + 2] != "\\\n":
                boundary = False
            pos += 2  # a continued line is not a fresh comment-line boundary
            continue
        # Inline comments are not masked, but their quotes must not alter lexical state.
        if char == "#" and boundary:
            end = command.find("\n", pos)
            pos = len(command) if end == -1 else end
            continue
        if command.startswith("<<<", pos):
            pos += 3
            continue
        if command.startswith("<<", pos):
            # Only complete, simply quoted delimiters are proven. Unquoted bodies
            # join backslash-newlines before recognizing a terminator; ANSI/mixed
            # quoting needs its own delimiter expansion grammar. Leave those alone.
            parsed = _HEREDOC.match(command, pos)
            if parsed is None:
                return command
            tabs, single, double = parsed.groups()
            heredocs.append((single if single is not None else double, bool(tabs)))
            pos = parsed.end()
            continue
        opening = _OPEN.match(command, pos)
        if opening is not None:
            end = _opaque_end(command, pos, opening.group())
            if end is None:
                return command
            pos = end
            boundary = False
        else:
            boundary = char in " \t;&|<>())"
            pos += 1
    return "".join(chars)
