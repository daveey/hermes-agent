"""Shell tokenization of command text, with executable expansions walked as their own commands."""
from __future__ import annotations

import shlex
from collections.abc import Iterator

from cron.lifecycle_guard_groups import (
    protect_redirection_ampersands, restore_redirection_ampersands, split_glued_redirections,
)
from cron.lifecycle_guard_lines import _split_logical_lines, strip_shell_comments
from cron.lifecycle_guard_substitutions import (
    MAX_SUBSTITUTION_DEPTH, SubstitutionNestingLimit, normalize_ansi_c_quotes, separate_substitutions,
)


def _shlex_tokens(line: str) -> list[str]:
    """POSIX-tokenize with word-start comments and redirection ampersands protected."""
    lexer = shlex.shlex(protect_redirection_ampersands(strip_shell_comments(line)),
                        posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [restore_redirection_ampersands(token) for token in lexer]


def _iter_command_token_lists(command: str, *, _nesting: int = 0) -> Iterator[list[str]]:
    """Yield the shell tokens of each logical line; a line shlex rejects (unbalanced quotes) falls
    back to per-physical-line tokenization. An unquoted ``<``/``>`` glued to a word starts a word
    of its own (``python3 -<f.py``), as in the shell."""
    if _nesting >= MAX_SUBSTITUTION_DEPTH:
        raise SubstitutionNestingLimit('substitution nesting limit reached')
    # Separate nested quote frames before any flat comment/quote preprocessing.
    # A quoted hash inside $(...) is data even inside an outer double quote.
    outer, bodies = separate_substitutions(command)
    for body in bodies:
        yield from _iter_command_token_lists(body, _nesting=_nesting + 1)
    # Keep both present-word and absent-word readings, plus the original evidence.
    if outer != command:
        yield from _iter_plain_token_lists(command)
        empty_outer, _ = separate_substitutions(command, empty_unquoted=True)
        if empty_outer != outer:
            yield from _iter_plain_token_lists(empty_outer)
    yield from _iter_plain_token_lists(outer)


def _iter_plain_token_lists(command: str) -> Iterator[list[str]]:
    """Tokenize without evaluating or separating any expansion."""
    command = strip_shell_comments(command).replace("\\\n", "")
    normalized = normalize_ansi_c_quotes(command)
    # Normalization helps shlex with Bash quoting, but must not erase evidence
    # recovered by the original physical-line fallback (the line splitter has
    # different backslash-in-single-quote rules). Walk both only when different.
    if normalized != command:
        yield from _iter_raw_token_lists(command)
    yield from _iter_raw_token_lists(normalized)


def _iter_raw_token_lists(command: str) -> Iterator[list[str]]:
    """Preserve the original logical/physical-line fallback without rewriting."""
    for line in _split_logical_lines(command):
        try:
            tokens = _shlex_tokens(split_glued_redirections(line))
        except ValueError:
            for physical_line in line.splitlines():
                try:
                    yield _shlex_tokens(split_glued_redirections(physical_line))
                except ValueError:
                    continue
            continue
        yield tokens
