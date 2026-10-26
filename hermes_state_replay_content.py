"""Canonical user provenance on the session-store replay projection."""


def replay_content_fields(row, decode_content, loaded_view_content, decode_metadata):
    """Keep native canonical user content; retain uncertainty for legacy rewrites.

    A native sidecar separates human content from API-only injections. Stripping
    memory tags from that canonical content erases authentic user restrictions.
    Wire replay still uses the unchanged sidecar; legacy replay keeps its lens.
    """
    original = decode_content(row["content"])
    native_user = row["role"] == "user" and bool(row["api_content"])
    content = original if native_user else loaded_view_content(row["role"], original)
    msg = {"role": row["role"], "content": content}
    metadata = decode_metadata(row["display_metadata"])
    prior_uncertainty = isinstance(metadata, dict) and metadata.get("user_content_sanitized")
    if row["role"] == "user" and (prior_uncertainty or (not native_user and isinstance(original, str)
            and content != original.strip())):
        # Local-only marker: erased scope must not look like complete authority.
        msg["_user_content_sanitized"] = True
    return msg


def replay_persistence_metadata(msg):
    """Carry lost-scope uncertainty through native compaction/rotation writes."""
    import json
    metadata = msg.get("display_metadata")
    if not msg.get("_user_content_sanitized"):
        return metadata
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return dict(metadata or {}, user_content_sanitized=True)
