"""``*_key`` names that hold identifiers, never credentials. [daveey]

Their values are hashes or ``kind:ref:<sha>`` strings shaped like an opaque token, so the assignment passes'
value-shape test masked ``idempotency_key='review:pr1305:<sha>'`` inside a Kanban wake_check, and the stored
probe could never match (t_01a1cf477dece56b). Exact names only, any case: ``my_cache_key_secret`` is not on
the list, and every credential name (``api_key``, ``secret_key``, ``private_key``, ``access_key``) still masks.
"""
from __future__ import annotations

IDENTIFIER_KEY_NAMES = frozenset({
    "idempotency_key", "cache_key", "sort_key", "primary_key", "partition_key", "dedupe_key", "dedup_key",
})
