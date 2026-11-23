"""Two independent session revision windows, bounded in one read. [daveey]"""
from dataclasses import replace

import hermes_state_session_change as listing
import hermes_state_session_content_change as content
from hermes_cli.fact_capture import CaptureRefused


def coverage(conn, anchor):
    from hermes_cli.fact_session_source import SessionCursor
    _, left = listing.changed_since(conn, None, limit=1)
    _, right = content.changed_since(conn, None, limit=1)
    if left is None or right is None:
        raise CaptureRefused("bootstrap session revision coverage lost")
    fresh = SessionCursor(left, right)
    if anchor is not None:
        for old, new in ((anchor.listing, left), (anchor.content, right)):
            if replace(new, revision=old.revision) != old or new.revision < old.revision:
                raise CaptureRefused("bootstrap session revision coverage lost")
    return fresh


def page(conn, anchor, position, fence, limit):
    fresh = coverage(conn, anchor)
    fence = fence or fresh
    for old, new in ((fence.listing, fresh.listing), (fence.content, fresh.content)):
        if replace(new, revision=old.revision) != old or new.revision < old.revision:
            raise CaptureRefused("bootstrap session replay fence lost")
    positions = list(position or (None, None))
    keys = []
    exhausted = True
    for index, (module, start, stop) in enumerate((
            (listing, anchor.listing, fence.listing),
            (content, anchor.content, fence.content))):
        remaining = limit - len(keys)
        if not remaining:
            exhausted = False
            break
        lower = positions[index] or (start.revision, "")
        # WITHOUT ROWID: the secondary rev index carries session_id as its
        # implicit tie-breaker, on both existing schemas (no writer migration).
        rows = tuple(conn.execute(
            f"SELECT rev,session_id FROM {module.CHANGE_TABLE} "
            f"INDEXED BY idx_{module.CHANGE_TABLE}_rev "
            "WHERE (rev,session_id) > (?,?) AND rev>? AND rev<=? "
            "ORDER BY rev,session_id LIMIT ?", (*lower, start.revision, stop.revision, remaining)))
        if rows:
            positions[index] = tuple(rows[-1])
        keys.extend(row[1] for row in rows)
        if len(rows) == remaining:
            exhausted = False
    return tuple(sorted(set(keys))), fresh, fence, tuple(positions), exhausted
