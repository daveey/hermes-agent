"""Process-local provenance for live human steering, not display-text inference.

Serialized history cannot mint authority. Copies within the current process retain
this attestation; a restart intentionally cannot authenticate historical steers.
"""
import hashlib
import hmac
import json
import secrets

_KEY = secrets.token_bytes(32)
FIELD = "_native_steer"


def _signature(row, text):
    payload = [row.get("role"), row.get("content"), row.get("display_kind"),
               row.get("message_uid"), text]
    return hmac.new(_KEY, json.dumps(payload, ensure_ascii=True).encode(), hashlib.sha256).hexdigest()


def attest_steering(row, text):
    from agent.message_metadata import stamp_message_uid
    stamp_message_uid(row)
    row[FIELD] = {"text": text, "signature": _signature(row, text)}
    return row


def steering_content(row):
    """Return native human text only for an intact live attestation."""
    if not isinstance(row, dict) or row.get("role") != "user" or row.get("display_kind") != "steer":
        return None
    proof = row.get(FIELD)
    if not isinstance(proof, dict):
        return None
    text, signature = proof.get("text"), proof.get("signature")
    if not isinstance(text, str) or not isinstance(signature, str):
        return None
    if not hmac.compare_digest(signature.encode(), _signature(row, text).encode()):
        return None
    return text


def flush_steering_before_compaction(agent, messages):
    """Do not summarize away accepted input before its first durable flush.

    The post-tool steer lands after the batch's incremental flush. In-place
    compaction otherwise assumes every dropped row is already archived. Use
    the normal identity-aware flush; do not expand the compression watermark
    over concurrently appended input. Failure aborts compaction, not retention.
    """
    pending = [row for row in messages if steering_content(row) is not None
               and not row.get("_db_persisted")]
    if not pending:
        return
    if getattr(agent, "_session_db", None) is None:
        raise RuntimeError("Cannot compact unpersisted human steering without storage")
    idx = getattr(agent, "_persist_user_message_idx", None)
    history = messages[:idx] if type(idx) is int and 0 <= idx <= len(messages) else []
    if not agent._flush_messages_to_session_db(messages, conversation_history=history):
        raise RuntimeError("Cannot compact unpersisted human steering")
