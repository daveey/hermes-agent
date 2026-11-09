"""Subprocess-diagnostic argv masking for agent.redact (``Command '[...]' timed out after``)."""

import re


# ``subprocess.TimeoutExpired`` / ``CalledProcessError`` text echoes the whole argv
# (``Command '['grep', '-rlF', '<secret>', '.']' timed out after 30 seconds``). An argv element has
# no key, so the only signal is its shape: a random token (agent.redact._RANDOM_TOKEN_RE shape, ``/`` excluded so
# path segments stay separate) inside that exact diagnostic is masked. Search for a secret
# in-process instead of passing it in argv: argv is also visible in ``ps`` and /proc.
_SUBPROCESS_COMMAND_TAIL_RE = re.compile(r"(['\"]) (?:timed out after |returned non-zero exit status |died with )")
_SUBPROCESS_COMMAND_HEAD_RE = re.compile(r"Command ['\"]")
_SUBPROCESS_COMMAND_SPAN = 16384  # a shell=True script may span lines
_ARGV_RANDOM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_+-])(?=[A-Za-z0-9_+-]*[a-z])(?=[A-Za-z0-9_+-]*[A-Z])[A-Za-z0-9_+-]{32,}={0,2}"
    r"(?![A-Za-z0-9_+=-])")


def _redact_subprocess_argv(text: str) -> str:
    """Mask random tokens between ``Command '`` and its ``' timed out after`` (etc.) tail.

    Anchored on the tail, then a bounded backward search for the head: a forward
    lazy regex from every ``Command '`` rescanned the line per head (quadratic on hostile output).
    ``scanned`` keeps the backward windows disjoint, so the whole pass is linear."""
    out, pos, scanned = [], 0, 0
    in_command = False
    for m in _SUBPROCESS_COMMAND_TAIL_RE.finditer(text):
        end = m.start()
        lo = max(scanned, end - _SUBPROCESS_COMMAND_SPAN)
        scanned = m.end()
        # A shell script can contain a tail with a different quote from the
        # diagnostic's head, or a fake Command head. Start at the first head
        # of either quote style so script text cannot hide earlier tokens.
        head = _SUBPROCESS_COMMAND_HEAD_RE.search(text, lo, end)
        if in_command:
            # The earlier tail may itself be script text. Continue masking in
            # this disjoint, bounded window, even across another script head.
            body = lo
        elif head is not None:
            body = head.end()
            in_command = True
        else:
            continue
        out += [text[pos:body], _ARGV_RANDOM_TOKEN_RE.sub("***", text[body:end])]
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)
