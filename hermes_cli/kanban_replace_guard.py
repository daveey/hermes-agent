"""Reject destructive replacement of Kanban identities at Python SQL entry points.

This is a public-factory invariant, not a sandbox for arbitrary SQLite callers or
trigger programs. SQLite REPLACE deletes implicitly even when recursive triggers
are disabled, bypassing the existing protected-row DELETE fences (PR4045).
"""
from __future__ import annotations

import re
import sqlite3
from functools import lru_cache


class ProtectedRowReplaceError(sqlite3.IntegrityError):
    """A caller attempted destructive replacement of a task or run identity."""


# Mirror SQLite's tokenizer (tokenize.c): whitespace is ASCII space/tab/LF/FF/CR plus
# a UTF-8 BOM where a token starts (CC_BOM -> TK_SPACE). Inside an identifier every
# non-ASCII character, the BOM included, is an identifier character: ``tasksé`` is one
# identifier, while ``\ufefftasks`` is whitespace + ``tasks``.
_WS = ' \t\n\f\r\ufeff'
_ID_START = '[A-Za-z_\u0080-\ufefe\uff00-\U0010ffff]'
_ID_CHAR = '[A-Za-z_0-9$\u0080-\U0010ffff]'
_TOKENS = re.compile(
    r"--[^\n]*(?:\n|$)|/\*.*?(?:\*/|$)|"
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^]]*\]|"
    + _ID_START + _ID_CHAR + "*|[^" + _WS + "]", re.DOTALL,
)


def _tokens(sql):
    for match in _TOKENS.finditer(sql):
        token = match.group()
        if not token.startswith(('--', '/*')):
            yield token


def _identifier(token):
    if token[:1] in ('"', "'", '`'):
        return token[1:-1].replace(token[0] * 2, token[0]).lower()
    if token.startswith('['):
        return token[1:-1].lower()
    return token.lower()


def reject_replace(sql):
    """Preflight the complete SQL before SQLite can write or implicitly commit."""
    # Only immutable built-in strings: subclass equality/hash cannot certify SQL.
    # Bound retained text as well as entries; parameters and DB results never enter.
    if type(sql) is str and len(sql) <= 4096:
        return _checked_syntax(sql)
    return _parse_replace(sql)


@lru_cache(maxsize=256)
def _checked_syntax(sql):
    # functools never caches exceptions: every refused statement still raises.
    return _parse_replace(sql)


def _parse_replace(sql):
    if not isinstance(sql, str):
        return  # preserve sqlite3's native type error
    tokens = list(_tokens(sql))
    words = [token.upper() for token in tokens]
    for index, word in enumerate(words):
        start = index + 1
        update = word == 'UPDATE' and words[start:start + 2] == ['OR', 'REPLACE']
        if word in ('INSERT', 'UPDATE') and words[start:start + 2] == ['OR', 'REPLACE']:
            start += 2
        elif word != 'REPLACE':
            continue
        if not update:
            if words[start:start + 1] != ['INTO']:
                continue
            start += 1
        if start >= len(tokens):
            continue
        if tokens[start + 1:start + 2] == ['.'] and start + 2 < len(tokens):
            start += 2
        table = _identifier(tokens[start])
        if table in ('tasks', 'task_runs'):
            raise ProtectedRowReplaceError(f'REPLACE is forbidden on Kanban {table}')


class _CursorMixin:
    def execute(self, sql, parameters=(), /):
        reject_replace(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        reject_replace(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql, /):
        reject_replace(sql)
        return super().executescript(sql)


_CURSOR_FACTORIES = {}


def _cursor_factory(factory):
    if issubclass(factory, _CursorMixin):
        return factory
    if factory not in _CURSOR_FACTORIES:
        _CURSOR_FACTORIES[factory] = type(
            f'ReplaceGuarded{factory.__name__}', (_CursorMixin, factory), {},
        )
    return _CURSOR_FACTORIES[factory]


class ReplaceGuardMixin:
    """Compose outside custom connection factories, retaining their SQL methods."""

    def cursor(self, factory=sqlite3.Cursor):
        return super().cursor(_cursor_factory(factory))

    def execute(self, sql, parameters=(), /):
        reject_replace(sql)
        # Native shortcuts return plain cursors; use our guarded cursor explicitly.
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        reject_replace(sql)
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql, /):
        reject_replace(sql)
        return self.cursor().executescript(sql)
