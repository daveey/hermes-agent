"""Publish a live replica read without duplicating its all-row aggregates. [daveey]"""


def finish(view, now, me, info_rows, last_ok, offline, key, stale, *, qualified_out, owners_out,
           moved_out, closed_out):
    from hermes_cli import kanban_session_activity as a
    from hermes_cli import kanban_replica_projection as projection

    aggregates = {}
    cards = projection.project(view, now, me, info_rows, last_ok, offline, fresh=key[3],
                               aggregates_out=aggregates)
    qualified = aggregates.get('qualified', frozenset())
    owners = aggregates.get('owners', frozenset())
    closed = aggregates.get('closed', frozenset())
    moved = aggregates.get('moved', {})
    seen_at = aggregates.get('seen', {})
    memos = dict(a._remote_read_memo or {})
    memos.pop((key[0], me, key[6]), None)
    memos[(key[0], me, key[6])] = (key, now, None, list(cards), qualified, owners, stale,
                                   a._replica_synced(seen_at, last_ok), seen_at, moved, closed)
    while len(memos) > a._REMOTE_READ_MEMOS:
        del memos[next(iter(memos))]
    a._remote_read_memo = memos
    for target, value in ((qualified_out, qualified), (owners_out, owners), (moved_out, moved),
                          (closed_out, closed)):
        if target is not None:
            target.update(value)
    return cards
