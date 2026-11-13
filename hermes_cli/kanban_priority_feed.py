"""Validate the finite feed interval covering a raw paged priority inventory."""
from __future__ import annotations

_CHANGED = 'canonical inventory changed; capture a new manifest'
_TERMINAL = ('done', 'archived')


def check_interval(hub, board: str, rows: list[dict], first: int, target: int) -> None:
    """Bring the paged inventory to its state at ONE fixed head, ``target``.

    The Board pages at most 1000 rows per call and holds no read transaction
    across calls, so each page reflects its own read time. Its consistent-read
    contract is: keep the FIRST page head and replay the dense feed from there.
    Replay runs from ``first`` through the final observed ``target`` (never a
    moving head that could starve forever), so the result is the inventory at
    ``target`` by construction:

    - status is replayed onto the rows (a card finishing or reopening);
    - a card CREATED in the window (its first in-window change is the Board's
      full creation row, ``version`` 1) joins the inventory even when its id
      sorts into a page read before it existed. Terminal at ``target`` it is
      kept as a terminal row, exactly as a later page would have shown it, so
      it is outside the manifest rows but still resolves an ``exceptions``
      entry whatever the page timing. Open at ``target`` it needs an origin and
      an integer priority <= ``PRIORITY_CAP``. That is fail-closed on purpose:
      the same card paged after its creation would be planned and normalized,
      but one that appeared after the first head is an arrival, and this
      mirrors ``kanban_priority_inventory.classify``'s apply-time arrival rule
      instead of normalizing a card the capture never paged.

    Identity, origin and priority are never replayed, so the result is the
    ``target`` inventory only when every in-window change carries the row's
    value. A row paged after its ONLY in-window priority change already holds
    the ``target`` value and passes. Any other in-window value that differs
    from the row refuses: a priority change on a page read before it, an
    earlier full-row upsert with the old priority (a creation row or a title
    edit followed by a re-prioritisation), and any origin or identity change.
    Deletes refuse too, as does a card missing from the snapshot that is open
    (or of unknown status) at ``target`` without a creation row (it existed
    before ``first``); terminal there, such a card stays out of the rows. No
    import or priority clipping occurs. Local apply still requires atomic CAS.
    """
    inventory = {row['id']: row for row in rows}
    created: dict[str, dict] = {}
    final: dict[str, str | None] = {}
    cursor = first
    for _ in range(10000):
        if cursor >= target:
            break
        page = hub.changes(board, cursor)
        batch, nxt = page.get('changes'), page.get('next')
        if (not isinstance(batch, list) or not batch or type(nxt) is not int
                or type(page.get('more')) is not bool):
            raise ValueError('invalid canonical feed page')
        previous = cursor
        for change in batch:
            if (not isinstance(change, dict) or type(change.get('seq')) is not int
                    or change['seq'] <= previous or not isinstance(change.get('table'), str)):
                raise ValueError('invalid canonical feed ordering')
            previous = change['seq']
            if previous <= target and change['table'] == 'tasks':
                _check_task(inventory, created, final, change)
        if nxt != previous:
            raise ValueError('invalid canonical feed cursor')
        cursor = nxt
        if cursor < target and not page['more']:
            raise ValueError('canonical feed ended before observed head')
    else:
        raise ValueError('canonical feed page limit exceeded')
    for uid, status in final.items():
        row = inventory.get(uid)
        if row is None:
            if status not in _TERMINAL:
                raise ValueError(_CHANGED)
        elif status is not None:
            row['status'] = status
    from hermes_cli.kanban_priority import PRIORITY_CAP
    for row in created.values():
        origin, value = row.get('created_on'), row.get('priority')
        if row['status'] not in _TERMINAL and (
                not isinstance(origin, str) or not origin or type(value) is not int or value > PRIORITY_CAP):
            raise ValueError(f'{_CHANGED} (new open card exceeds cap or lacks origin)')
        rows.append(row)
    rows.sort(key=lambda row: row['id'])


def _creation(uid: str, fields: dict) -> bool:
    """The Board's full insert row: it writes version 1 only when it creates a row."""
    return (type(fields.get('version')) is int and fields['version'] == 1
            and fields.get('id') == uid and 'status' in fields)


def _check_task(inventory: dict, created: dict, final: dict, change: dict) -> None:
    uid = change.get('uid')
    fields = change.get('fields')
    if not isinstance(uid, str) or change.get('op') != 'upsert' or not isinstance(fields, dict):
        raise ValueError(_CHANGED)
    # Full-row and patch-shaped feed entries are both accepted; a patch without
    # a status leaves the last replayed (or snapshot) status in place.
    from hermes_cli.kanban_priority_manifest import STATUSES
    if 'status' in fields and (not isinstance(fields['status'], str) or fields['status'] not in STATUSES):
        raise ValueError(_CHANGED)
    if uid not in inventory and uid not in final and _creation(uid, fields):
        # Only the FIRST in-window change admits a missing card: a card first
        # seen as an update existed before ``first`` and belonged in the snapshot.
        inventory[uid] = created[uid] = dict(fields)
    if 'status' in fields:
        final[uid] = fields['status']
    else:
        final.setdefault(uid, None)
    row = inventory.get(uid)
    if row is None:
        return
    for key in ('id', 'created_on', 'priority'):
        if key in fields and (type(fields[key]) is not type(row.get(key)) or fields[key] != row.get(key)):
            raise ValueError(_CHANGED)
