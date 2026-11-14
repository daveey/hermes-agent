"""Pure decoding of cached remote task rows for session activity."""
from __future__ import annotations

import json
from typing import Any

from hermes_cli.kanban_session_activity_release import TERMINAL_STATUSES


def _decode_replica_task(record_key: str, data: Any) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """``(task, legacy record digest or None, owner evidence)`` of one replica row; a pure function of its text.
    Callers treat ``task`` as read-only: it is shared across reads of an unchanged row."""
    from hermes_cli.kanban_origin_ledger import _owner_evidence
    from hermes_cli.kanban_origin_legacy import record_digest

    try:
        t = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid replica task data') from exc
    if not isinstance(t, dict) or not t.get('id'):
        raise ValueError('invalid replica task data')
    digest = record_digest(t) if len(str(record_key).split('/')) == 2 else None
    evidence = _owner_evidence(t) if t.get('status') in TERMINAL_STATUSES else {}
    return t, digest, evidence
