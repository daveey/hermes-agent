"""Shell tokenization of command text, with executable expansions walked as their own commands."""
from __future__ import annotations

import shlex
from collections.abc import Iterator

from cron.lifecycle_guard_groups import (
    protect_redirection_ampersands, restore_redirection_ampersands,
)
from cron.lifecycle_guard_lines import _split_logical_lines, strip_shell_comments
from cron.lifecycle_guard_words import REDIRECTION, tokenize_redirections
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
    yield from _iter_memoized_token_lists(command, _nesting, {})


def _iter_memoized_token_lists(
    command: str, nesting: int, bodies_cache: dict[tuple[str, int], tuple[tuple[str, ...], ...]],
) -> Iterator[list[str]]:
    if nesting >= MAX_SUBSTITUTION_DEPTH:
        raise SubstitutionNestingLimit('substitution nesting limit reached')
    # Separate nested quote frames before any flat comment/quote preprocessing.
    # A quoted hash inside $(...) is data even inside an outer double quote.
    outer, bodies = separate_substitutions(command)
    for body in bodies:
        # The same body at a deeper level has less budget: text alone is not
        # a safe cache key. Replay fresh lists to preserve occurrence order and
        # prevent a consumer's mutation from corrupting later occurrences.
        key = (body, nesting + 1)
        if key not in bodies_cache:
            bodies_cache[key] = tuple(tuple(tokens) for tokens in
                                      _iter_memoized_token_lists(body, nesting + 1, bodies_cache))
        for tokens in bodies_cache[key]:
            yield list(tokens)
    # Keep both present-word and absent-word readings, plus the original evidence.
    seen_readings: set[str] = set()
    if outer != command:
        yield from _iter_plain_token_lists(command, seen_readings)
        empty_outer, _ = separate_substitutions(command, empty_unquoted=True)
        if empty_outer != outer:
            yield from _iter_plain_token_lists(empty_outer, seen_readings)
    yield from _iter_plain_token_lists(outer, seen_readings)


def _iter_plain_token_lists(command: str, seen: set[str] | None = None) -> Iterator[list[str]]:
    """Tokenize without evaluating or separating any expansion."""
    command = strip_shell_comments(command).replace("\\\n", "")
    normalized = normalize_ansi_c_quotes(command)
    # Normalization helps shlex with Bash quoting, but must not erase evidence
    # recovered by the original physical-line fallback (the line splitter has
    # different backslash-in-single-quote rules). Walk both only when different.
    if seen is None:
        seen = set()
    for reading in (command, normalized):
        if reading not in seen:
            seen.add(reading)
            yield from _iter_raw_token_lists(reading)


def _iter_raw_token_lists(command: str) -> Iterator[list[str]]:
    """Preserve the original logical/physical-line fallback without rewriting."""
    for line in _split_logical_lines(command):
        try:
            tokens = tokenize_redirections(line, _shlex_tokens, REDIRECTION)
        except ValueError:
            for physical_line in line.splitlines():
                try:
                    yield tokenize_redirections(physical_line, _shlex_tokens, REDIRECTION)
                except ValueError:
                    continue
            continue
        yield tokens
