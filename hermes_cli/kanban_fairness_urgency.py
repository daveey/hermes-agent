"""Sanitize urgency observations without changing the urgency reader's policy."""
import base64
import hashlib
import json

from hermes_cli.kanban_fairness_payloads import MAX_SOURCE_BYTES, _validate_source_document
from hermes_cli.kanban_fairness_schema import BadRecord


def receipt(*, board, now_s, raw, outcome, owners):
    safe = False
    value = {}
    if raw is not None and len(raw) <= MAX_SOURCE_BYTES:
        try:
            _validate_source_document(raw)
            safe = True
            value = json.loads(raw)
        except BadRecord:
            pass
    # Reader accepts some source documents the evidence privacy allowlist forbids.
    # Preserve actual owner_ids, but never pretend the exact-byte gate was met.
    reason = None
    if outcome in {"valid", "expired"} and not safe:
        outcome, reason = "invalid", "source_not_allowlisted"
    retain = safe and outcome != "invalid"
    return dict(receipt_type="urgency_read", board=board, read_at_s=now_s, outcome=outcome,
                raw_b64=base64.b64encode(raw).decode("ascii") if retain else None,
                raw_sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None,
                raw_size=len(raw) if raw is not None else None,
                raw_complete=raw is not None and len(raw) <= MAX_SOURCE_BYTES,
                redacted=raw is not None and not retain,
                observed_at=value.get("observed_at"), valid_until=value.get("valid_until"),
                source_revision=value.get("source_revision"), owner_ids=sorted(owners), reason=reason)


def emit(**observation):
    from hermes_cli import kanban_fairness_runtime as runtime

    try:
        payload = receipt(**observation)
        runtime.submit(board=observation["board"], kind="owner_generation", payload=payload)
    except (ValueError, TypeError, OverflowError, RecursionError, OSError, RuntimeError):
        # Do not retry a failed sink: it may fail again and change the read result.
        # The in-memory invalidation prevents an apparently complete epoch seal.
        runtime.invalidate(board=observation["board"])
