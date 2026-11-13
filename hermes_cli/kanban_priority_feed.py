"""Validate the finite feed interval covering a raw paged priority inventory."""
from __future__ import annotations

_CHANGED = 'canonical inventory changed; capture a new manifest'
_TERMINAL = ('done', 'archived')


def check_interval(hub, board: str, rows: list[dict], first: int, target: int) -> None:
    """Ignore unrelated traffic; bring the inventory to its state at ``target``.

    The snapshot is not transactional: each page reflects its own read time, so
    a row paged late can already carry a later state than an intermediate feed
    change. Replay from the FIRST head through the final observed head (never a
    moving head that could starve forever) and compare the FINAL replayed state
    with each snapshot row, not every intermediate change.

    Status is replayed onto the rows, so an open<->terminal flip inside the
    window (a card finishing or reopening) yields the inventory at ``target``.
    Identity, origin and priority are never replayed: any in-window value that
    differs from the row refuses, as do deletes and a card missing from the
    snapshot that is open (or of unknown status) at ``target``. A card missing
    from the snapshot that is terminal at ``target`` (created and finished in
    the window) is out of manifest scope. No import or priority clipping
    occurs. Local apply still requires atomic CAS.
    """
    inventory = {row['id']: row for row in rows}
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
                _check_task(inventory, final, change)
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


def _check_task(inventory: dict, final: dict, change: dict) -> None:
    uid = change.get('uid')
    fields = change.get('fields')
    if not isinstance(uid, str) or change.get('op') != 'upsert' or not isinstance(fields, dict):
        raise ValueError(_CHANGED)
    # Full-row and patch-shaped feed entries are both accepted; a patch without
    # a status leaves the last replayed (or snapshot) status in place.
    from hermes_cli.kanban_priority_manifest import STATUSES
    if 'status' in fields:
        if not isinstance(fields['status'], str) or fields['status'] not in STATUSES:
            raise ValueError(_CHANGED)
        final[uid] = fields['status']
    else:
        final.setdefault(uid, None)
    row = inventory.get(uid)
    if row is None:
        return
    for key in ('id', 'created_on', 'priority'):
        if key in fields and (type(fields[key]) is not type(row.get(key)) or fields[key] != row.get(key)):
            raise ValueError(_CHANGED)
