"""The diagnostic client rejects mixed/truncated/tampered page streams."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import code_provenance_client as client
from hermes_cli.code_attestation_snapshot import ENCODER, SECTIONS


@pytest.fixture
def stream(monkeypatch):
    data = {name: [] for name in SECTIONS}
    data['unknowns'] = ['one', 'two']
    manifest = {name: {'count': len(rows), 'sha256': hashlib.sha256(ENCODER.encode(rows).encode()).hexdigest()}
                for name, rows in data.items()}
    summary = {'complete': True, 'purpose': 'diagnostic_only', 'snapshot_id': 'a' * 64,
               'captured': {'generation': 1}, 'sections': manifest, 'raw': manifest['unknowns']}
    def query(home, params):
        if params['op'] == 'capture':
            return copy.deepcopy(summary)
        section, offset = params['section'], params['offset']
        rows = data[section][offset:offset + 1]
        return {**copy.deepcopy(summary), 'section': section, 'offset': offset, 'entries': rows,
                'next': offset + len(rows) if offset + len(rows) < len(data[section]) else None}
    monkeypatch.setattr(client, 'query', query)
    return query, data, summary


def test_client_verifies_all_sections(stream):
    assert client.collect(Path('/unused'), {})['sections'] == stream[1]


@pytest.mark.parametrize('field,value', [('complete', False), ('purpose', 'authorization'),
    ('snapshot_id', 'b' * 64), ('captured', {'generation': 2}), ('offset', 99), ('offset', False), ('section', 'other'),
    ('sections', {}), ('entries', []), ('entries', ['tampered']), ('next', 99), ('next', True)])
def test_client_refuses_altered_pages(stream, monkeypatch, field, value):
    original = stream[0]
    def corrupt(home, params):
        result = original(home, params)
        if params['op'] == 'page' and params['section'] == 'unknowns':
            result[field] = value
        return result
    monkeypatch.setattr(client, 'query', corrupt)
    with pytest.raises(ValueError):
        client.collect(Path('/unused'), {})


@pytest.mark.parametrize('field,value', [('complete', False), ('purpose', 'authorization'), ('raw', {})])
def test_client_refuses_bad_capture(stream, field, value):
    stream[2][field] = value
    with pytest.raises(ValueError):
        client.collect(Path('/unused'), {})


def test_client_has_total_byte_limit(stream, monkeypatch):
    monkeypatch.setattr(client, 'MAX_SNAPSHOT_BYTES', 4100)
    with pytest.raises(ValueError, match='too_large'):
        client.collect(Path('/unused'), {})


@pytest.mark.parametrize('mode', ['valid', 'eof', 'overflow', 'deadline', 'refused'])
def test_query_bounds_actual_reads_and_deadline(monkeypatch, mode):
    from hermes_cli import gateway_runtime_discovery
    payload = b'{"ok": true, "result": {"complete": true}}\n'
    if mode == 'refused':
        payload = b'{"ok": false}\n'
    class Peer:
        def __init__(self):
            self.offset = 0
            self.reads = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def sendall(self, request):
            assert json.loads(request)['verb'] == 'code-provenance'
        def settimeout(self, timeout):
            assert 0 < timeout <= 5
        def recv(self, amount):
            assert amount == 65536
            self.reads += 1
            assert self.reads <= 9, 'read loop escaped the actual-byte bound'
            if mode == 'eof':
                return b''
            if mode == 'overflow':
                return b'x' * amount
            chunk = payload[self.offset:self.offset + 8]
            self.offset += len(chunk)
            return chunk
    monkeypatch.setattr(gateway_runtime_discovery, 'connect_private', lambda *args: Peer())
    if mode == 'deadline':
        ticks = iter((0, 6))
        monkeypatch.setattr(client.time, 'monotonic', lambda: next(ticks))
    if mode == 'valid':
        assert client.query(Path('/unused'), {}) == {'complete': True}
    else:
        with pytest.raises(TimeoutError if mode == 'deadline' else ValueError):
            client.query(Path('/unused'), {})
