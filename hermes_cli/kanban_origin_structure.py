"""Bounded exact-content derivations for origin reads, never live verdicts.

Fresh SQL still supplies every raw value. No connection, projection, clock,
active stream, alias resolution or pending deadline is retained here.
"""
from __future__ import annotations

import json
from functools import lru_cache

_MAX_BINDING_CHARS = 4096


def _decode_binding(raw):
    from hermes_cli.kanban_origin_ledger import validate_binding

    value = json.loads(raw)
    validate_binding(value)  # exceptions are not cached
    return value


@lru_cache(maxsize=1024)
def _binding(raw: str):
    return _decode_binding(raw)


def binding(raw):
    """Return caller-owned validated structure; reuse only bounded exact text.

    Validation bounds each nested mapping to string scalars, so copying these
    two mappings is sufficient. Non-text/oversized inputs take the literal path
    (including its errors), rather than growing retained memory without bound.
    """
    value = (_binding(raw) if type(raw) is str and len(raw) <= _MAX_BINDING_CHARS
             else _decode_binding(raw))
    return {**value, 'owner': dict(value['owner']), 'origin': dict(value['origin'])}


@lru_cache(maxsize=1024)
def _digest(raw: str):
    from hermes_cli.kanban_origin_ledger import binding_hash

    return binding_hash(_binding(raw))


def digest(raw, value):
    """Hash only at the original consumer boundary, after earlier errors."""
    from hermes_cli.kanban_origin_ledger import binding_hash

    return (_digest(raw) if type(raw) is str and len(raw) <= _MAX_BINDING_CHARS
            else binding_hash(value))


def clear() -> None:
    """Retire all retained structural inputs (also useful for cold-read proofs)."""
    _binding.cache_clear()
    _digest.cache_clear()
