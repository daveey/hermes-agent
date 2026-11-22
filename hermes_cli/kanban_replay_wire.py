"""Canonical immutable replay envelope framing (RFC8785). [daveey]

Session is replaceable transport authority; it and the self digest are the only
excluded replay fields. All intent fields and a piggybacked ack remain hashed.
"""
from __future__ import annotations

import copy
import hashlib

import rfc8785


def _digest(wire: dict) -> str:
    body = copy.deepcopy(wire)
    body['replay'].pop('payload_sha256', None)
    body['replay'].pop('session', None)
    return hashlib.sha256(rfc8785.dumps(body)).hexdigest()


def freeze(wire: dict) -> dict:
    """Return an isolated first-send envelope with its canonical digest."""
    frozen = copy.deepcopy(wire)
    frozen['replay']['payload_sha256'] = _digest(frozen)
    return frozen


def verify(wire: dict) -> str:
    """Refuse changed intent bytes; never repair an issued digest."""
    digest = _digest(wire)
    if wire['replay'].get('payload_sha256') != digest:
        raise ValueError('replay digest mismatch')
    return digest
