"""String-heavy replica pages keep Python work structural, not per escape. [daveey]"""
import json

import pytest

from hermes_cli import kanban_replica_transport as transport


def test_escaped_payload_does_not_dispatch_each_escape_to_python(monkeypatch):
    tokens = transport._TOKENS
    visits = []

    class CountTokens:
        def finditer(self, *args):
            for match in tokens.finditer(*args):
                visits.append(1)
                yield match

        def search(self, *args):
            match = tokens.search(*args)
            if match is not None:
                visits.append(1)
            return match

    monkeypatch.setattr(transport, '_TOKENS', CountTokens())
    row = ['owner', 'p/b/t_one', '\\"[]{}\n' * 20000, 1000, 1, 1, 140000]
    packed = json.dumps([row], ensure_ascii=False).encode()
    assert list(transport.iter_page(packed)) == [tuple(row)]
    # Chunk count can grow with payload size; escaped punctuation must not cause
    # one Python token dispatch per byte of a serialized task/history string.
    assert len(visits) < 100, len(visits)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16le', 'utf-16be'])
@pytest.mark.parametrize('chunk', [1, 2, 3, 7, 31, 16384])
def test_chunk_boundaries_preserve_nested_strings_and_typed_cells(monkeypatch, encoding, chunk):
    monkeypatch.setattr(transport, '_CHUNK', chunk)
    strings = ['', '"', '\\', '\\"', '\\\\"', '[]{}', '\n\t\r\b\f', '😀é中',
               json.dumps({'body': '"\\[]{}😀\n' * 20}), '\\' * 33]
    rows = [['gw', 'p/b/t', text, None, -1, 1.25, {'nested': [text, True, False]}] for text in strings]
    packed = json.dumps(rows, ensure_ascii=False).encode(encoding)
    assert list(transport.iter_page(packed, encoding)) == [tuple(row) for row in rows]
    assert list(transport.iter_page('[]'.encode(encoding), encoding)) == []


@pytest.mark.parametrize('packed', [b'[["open', b'[["escape\\', b'[[1,2', b'[[{"k":'])
def test_truncated_page_still_refuses(packed):
    with pytest.raises(ValueError):
        list(transport.iter_page(packed))


def test_seeded_string_rows_match_full_decode(monkeypatch):
    import random

    rng = random.Random(435)
    alphabet = 'abc"\\[]{}\n\t😀é中'
    rows = [[i, ''.join(rng.choices(alphabet, k=rng.randrange(1500))), None, True] for i in range(120)]
    packed = json.dumps(rows, ensure_ascii=False).encode()
    for chunk in (1, 19, 16384):
        monkeypatch.setattr(transport, '_CHUNK', chunk)
        assert list(transport.iter_page(packed)) == [tuple(row) for row in rows]


def test_invalid_utf8_retains_sqlite_error():
    import sqlite3

    with pytest.raises(sqlite3.OperationalError, match='Could not decode replica page as text'):
        list(transport.iter_page(b'[["\xff"]]'))
