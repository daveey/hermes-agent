"""Supported private provenance client, with bounded authenticated reads and digest verification.

Invoke only against a gateway that has the diagnostic API installed; never installs
or changes the ledger. ``python -m hermes_cli.code_provenance_client HOME`` writes
the verified diagnostic document to stdout, not a persistent server snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from hermes_cli.code_attestation_snapshot import ENCODER, MAX_SNAPSHOT_BYTES, SECTIONS


def query(home: Path, params: dict, *, timeout: float = 5) -> dict:
    """One private request; retain the ordinary client deadline, bound actual bytes."""
    request = json.dumps({'protocol': 1, 'id': 1, 'verb': 'code-provenance', 'params': params}).encode() + b'\n'
    if os.name == 'nt':
        from gateway.runtime_bootstrap_windows import query_runtime_control
        raw = query_runtime_control(home, request, timeout)
    else:
        from hermes_cli.gateway_runtime_discovery import connect_private
        deadline = time.monotonic() + timeout
        data = bytearray()
        with connect_private(home, timeout) as peer:
            peer.sendall(request)
            while b'\n' not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('provenance_response_timeout')
                peer.settimeout(remaining)
                chunk = peer.recv(65536)
                if not chunk or len(data) + len(chunk) > 512 * 1024:
                    raise ValueError('provenance_response_invalid')
                data.extend(chunk)
        raw = bytes(data)
    reply = json.loads(raw)
    if not reply.get('ok'):
        raise ValueError('provenance_request_refused')
    return reply['result']


def collect(home: Path, expected: dict) -> dict:
    """Reconstruct every section in one short-lived capture, rejecting any gap/mixup."""
    summary = query(home, {'schema': 1, 'op': 'capture', 'expected': expected, 'refresh_inventory': True})
    if summary.get('complete') is not True or summary.get('purpose') != 'diagnostic_only':
        raise ValueError('provenance_capture_incomplete')
    result, total = {}, 4096
    for section in SECTIONS:
        rows, offset = [], 0
        while offset is not None:
            page = query(home, {'schema': 1, 'op': 'page', 'snapshot_id': summary['snapshot_id'],
                               'section': section, 'offset': offset, 'limit': 128})
            if (page.get('complete') is not True or page.get('purpose') != 'diagnostic_only'
                    or page.get('snapshot_id') != summary['snapshot_id']
                    or page.get('captured') != summary['captured'] or type(page.get('offset')) is not int
                    or page.get('offset') != offset
                    or page.get('section') != section or page.get('sections') != summary['sections']):
                raise ValueError('provenance_page_mismatch')
            entries, next_offset = page['entries'], page['next']
            if next_offset is not None and (not entries or type(next_offset) is not int
                                             or next_offset != offset + len(entries)):
                raise ValueError('provenance_cursor_invalid')
            for entry in entries:
                total += len(ENCODER.encode(entry)) + 1
                if total > MAX_SNAPSHOT_BYTES:
                    raise ValueError('provenance_snapshot_too_large')
                rows.append(entry)
            offset = next_offset
        digest = hashlib.sha256(ENCODER.encode(rows).encode('ascii')).hexdigest()
        if summary['sections'][section] != {'count': len(rows), 'sha256': digest}:
            raise ValueError('provenance_digest_mismatch')
        result[section] = rows
    if summary['raw'] != summary['sections']['unknowns']:
        raise ValueError('provenance_raw_mismatch')
    return {'summary': summary, 'sections': result}


def main():
    import sys
    from hermes_cli.gateway_client import private_control_query
    home = Path(sys.argv[1])
    reply = json.loads(private_control_query(home, b'{"protocol":1,"id":1,"verb":"identify"}\n'))
    if not reply.get('ok'):
        raise ValueError('identify_refused')
    expected = {k: reply['result'][k] for k in ('pid', 'start_time', 'instance_id')}
    print(ENCODER.encode(collect(home, expected)))


if __name__ == '__main__':
    main()
