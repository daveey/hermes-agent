"""``*_key`` names that hold identifiers, never credentials. [daveey]

Their values are hashes or ``kind:ref:<sha>`` strings shaped like an opaque token, so the assignment passes'
value-shape test masked ``idempotency_key='review:pr1305:<sha>'`` inside a Kanban wake_check, and the stored
probe could never match (t_01a1cf477dece56b). Exact names only, any case: ``my_cache_key_secret`` is not on
the list, and every credential name (``api_key``, ``secret_key``, ``private_key``, ``access_key``) still masks.
``primary_key``/``secondary_key`` are deliberately absent: Azure names its shared-access keys that way (azurerm
``*.primary_key``, azure-mgmt ``AccessKeys.primary_key``). A dotted name is judged on its last segment, so
``cosmos.primary_key=`` masks too.

``wake_check_secret`` is a Kanban refusal code, not a credential: its message starts ``wake_check_secret: this
wake_check ...``, and at a line start the YAML pass read that as an assignment and masked ``this``.
"""
from __future__ import annotations

IDENTIFIER_KEY_NAMES = frozenset({
    "idempotency_key", "cache_key", "sort_key", "partition_key", "dedupe_key", "dedup_key",
    "wake_check_secret",
})
