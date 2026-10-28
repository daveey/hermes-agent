"""Card identity helpers for local and remote session activity snapshots."""


def local_keys(path, reads):
    """Board-qualified identities from the count transaction, also retained by its memo."""
    parked = reads.get("parked_pairs", set())
    out = {}
    for bucket in ("active", "blocked", "held", "stalled"):
        for sid, tid in reads.get(bucket + "_pairs", ()):
            out.setdefault(sid, set()).add((f"{path}/{tid}", bucket, (sid, tid) in parked and bucket == "active"))
    return out


def merge_keys(*sources):
    out = {}
    for source in sources:
        for sid, keys in source.items():
            out.setdefault(sid, set()).update(keys)
    return {sid: frozenset(keys) for sid, keys in out.items()}


def lineage_duplicates(card_keys, ids):
    """Excess per-session counts for cards shared by aliases of one compression lineage."""
    seen = [card_keys.get(sid, frozenset()) for sid in ids]
    if sum(1 for s in seen if s) < 2:
        return {}
    union = frozenset().union(*seen)
    out = {}
    for field, test in (("kanban_active", lambda k: k[1] == "active"),
                        ("kanban_blocked", lambda k: k[1] == "blocked"),
                        ("kanban_held", lambda k: k[1] == "held"),
                        ("kanban_stalled", lambda k: k[1] == "stalled"),
                        ("kanban_parked", lambda k: k[2])):
        rows = sum(1 for s in seen for k in s if test(k))
        cards = len({k[0] for k in union if test(k)})
        if rows > cards:
            out[field] = rows - cards
    return out


def row_ids(row):
    seen = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


def freeze_annotation(parts):
    """Copy a read so a later recompute cannot mutate a poll still using it."""
    (active, blocked, unseen, remote_stale, waiting, parked, held, stalled, card_keys, extra,
     attention) = parts
    return (
        dict(active), dict(blocked), dict(unseen), dict(remote_stale),
        {sid: frozenset(ids) for sid, ids in waiting.items()},
        dict(parked), dict(held), dict(stalled),
        {sid: frozenset(keys) for sid, keys in card_keys.items()},
        {sid: frozenset(ids) for sid, ids in extra.items()},
        dict(attention),
    )
