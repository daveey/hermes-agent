"""Bounded decoding of SQLite's JSON-array replica pages. [daveey]"""
from __future__ import annotations

import codecs
import json
import re
import sqlite3

_TOKENS = re.compile(r'["\[\]{}]')
# Possessive runs skip serialized task strings in C without dispatching each escape.
_STRING = re.compile(r'(?:[^"\\]++|\\.)*+', re.DOTALL)
_CHUNK = 16 << 10


def iter_page(packed: bytes, encoding: str = 'utf-8'):
    """Yield seven-tuples from a SQLite-generated array without decoding a whole page.

    Scan delimiters once, then JSON-decode each complete row once. Fragments contain
    at most one row, so an oversized row does not cause repeated prefix parsing.
    Encoding is the caller's PRAGMA encoding (SQLite CAST(TEXT AS BLOB) uses it).
    """
    decoder = codecs.getincrementaldecoder(encoding)()
    depth, quoted, escaped = 0, False, False
    fragments = []
    try:
        for offset in range(0, len(packed), _CHUNK):
            chunk = decoder.decode(packed[offset:offset + _CHUNK], final=offset + _CHUNK >= len(packed))
            start = 0 if depth >= 2 else None
            pos = 0
            if escaped and chunk:
                pos, escaped = 1, False
            while pos < len(chunk):
                if quoted:
                    pos = _STRING.match(chunk, pos).end()
                    if pos == len(chunk):
                        break
                    if chunk[pos] == '\\':  # an escape split across decoded chunks
                        escaped = True
                        break
                    quoted = False  # the first unescaped quote closes the string
                    pos += 1
                    continue
                match = _TOKENS.search(chunk, pos)
                if match is None:
                    break
                token = match.group()
                pos = match.end()
                if token == '"':
                    quoted = True
                elif token in ('[', '{'):
                    if depth == 1:
                        start = match.start()
                    depth += 1
                elif token in (']', '}'):
                    depth -= 1
                    if depth == 1:
                        fragments.append(chunk[start:match.end()])
                        row = ''.join(fragments)
                        fragments.clear()
                        start = None
                        yield tuple(json.loads(row))
                        del row
            if start is not None:
                fragments.append(chunk[start:])
    except UnicodeDecodeError as exc:
        raise sqlite3.OperationalError('Could not decode replica page as text') from exc
    if depth or quoted or fragments:
        raise ValueError('incomplete replica JSON page')
