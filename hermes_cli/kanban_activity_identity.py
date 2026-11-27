"""Card identity helpers for local and remote session activity snapshots."""
from copy import deepcopy
from hermes_cli import kanban_session_activity_timers as timers


def local_keys(path, reads, board=None):
    """Board-qualified identities from the count transaction, also retained by its memo.

    ``board`` (the board slug) keys a card ``<board>/<task id>``: the local key core's ``parked_waits`` and the
    chat-closeout plugin's ``card_key`` use, so a pending close-out of a card the boards already count is
    recognised as the same card. Remote keys have at least three parts (``gateway/[profile/]board/id``) and a slug
    has no ``/``, so the two never collide. Without a slug the key falls back to the board's path."""
    label = board or path
    parked = reads.get("parked_pairs", set())
    out = {}
    for bucket in ("active", "blocked", "held", "stalled"):
        for sid, tid in reads.get(bucket + "_pairs", ()):
            out.setdefault(sid, set()).add((f"{label}/{tid}", bucket, (sid, tid) in parked and bucket == "active"))
    for sid, tid in reads.get('pairs', ()):
        card = reads.get('timers', {}).get(tid)
        if card:
            out.setdefault(sid, set()).update(timers.marker({**card, 'key': f'{label}/{tid}'}))
            from hermes_cli.kanban_triage_attention import marker
            out[sid].update(marker({**card, 'key': f'{label}/{tid}'}))
    return out


def is_local_board_key(key):
    """A ``<board slug>/<task id>`` key (see ``local_keys``), as opposed to a remote ``gateway/.../id`` key."""
    return key.count("/") == 1


def closeout_extra(open_ids, extra_ids, card_keys):
    """``(pending, listed)`` for plugin closeout ids against what the boards already count.

    ``open_ids``: the bare local ids counted (a legacy bare closeout id matches these). ``card_keys``: per-session
    ``(key, bucket, parked)`` sets, remote ``gateway/.../id`` and local ``<board>/<id>`` keys (the chat-closeout
    ``card_key`` form). ``pending`` are closeouts no counted card covers (they add to ``kanban_active``);
    ``listed`` are the closeout ids to show in ``kanban_waiting_on``: a counted local card is already listed under
    its bare id, so its scoped alias is dropped."""
    counted = {key for keys in card_keys for key, bucket, _ in keys if bucket in ("active", "blocked")}
    pending = set(extra_ids) - set(open_ids) - counted
    return pending, set(extra_ids) - {key for key in counted if is_local_board_key(key)}


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
     attention) = parts[:11]
    return (
        dict(active), dict(blocked), dict(unseen), dict(remote_stale),
        {sid: frozenset(ids) for sid, ids in waiting.items()},
        dict(parked), dict(held), dict(stalled),
        {sid: frozenset(keys) for sid, keys in card_keys.items()},
        {sid: frozenset(ids) for sid, ids in extra.items()},
        dict(attention),
    ) + ((deepcopy(parts[11]),) if len(parts) > 11 else ())


def stamp_blocked_owners(row, card_keys, ids, cap):
    """Name the blocked owners from the very snapshot that supplies the red count."""
    owners = sorted({key for sid in ids for key, bucket, _ in card_keys.get(sid, ())
                     if bucket == "blocked"})
    if owners:
        row["kanban_blocked_on"] = owners[:cap]
    else:
        row.pop("kanban_blocked_on", None)


def mark_unknown(rows):
    """Drop the counts and stamp ``kanban_unknown`` (a failed read is unknown, never \"no work\")."""
    for row in rows:
        for key in ("kanban_active", "kanban_blocked", "kanban_unseen", "kanban_attention", "kanban_remote_stale",
                    "kanban_parked", "kanban_held", "kanban_stalled", "kanban_triage_attention"):
            row.pop(key, None)
        row.pop("kanban_observed_at", None)
        row.pop("kanban_waiting_on", None)
        row.pop("kanban_blocked_on", None)
        row.pop("needs_you", None)
        row.pop("kanban_timed_cards", None)
        row.pop("kanban_timed_more", None)
        row["kanban_unknown"] = True
    return rows
