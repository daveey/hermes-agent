"""Validate the finite feed interval covering a raw paged priority inventory."""
from __future__ import annotations


def check_interval(hub, board: str, rows: list[dict], first: int, target: int) -> None:
    """Ignore unrelated traffic; refuse changed task membership or migration inputs.

    The snapshot is not transactional. Replay from its FIRST head through the
    final observed head, not through a moving head that could starve forever.
    No import or priority clipping occurs. Local apply still requires atomic CAS.
    """
    inventory = {row['id']: row for row in rows}
    cursor = first
    for _ in range(10000):
        if cursor >= target:
            return
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
                _check_task(inventory, change)
        if nxt != previous:
            raise ValueError('invalid canonical feed cursor')
        cursor = nxt
        if cursor < target and not page['more']:
            raise ValueError('canonical feed ended before observed head')
    raise ValueError('canonical feed page limit exceeded')


def _check_task(inventory: dict, change: dict) -> None:
    row = inventory.get(change.get('uid'))
    fields = change.get('fields')
    if row is None or change.get('op') != 'upsert' or not isinstance(fields, dict):
        raise ValueError('canonical inventory changed; capture a new manifest')
    # Full-row and patch-shaped feed entries are both accepted. Open-status
    # transitions do not change ranks; replay them for exception audit evidence.
    from hermes_cli.kanban_priority_manifest import STATUSES
    status = fields.get('status', row['status'])
    if (status not in STATUSES
            or (status in ('done', 'archived')) != (row['status'] in ('done', 'archived'))):
        raise ValueError('canonical inventory changed; capture a new manifest')
    row['status'] = status
    for key in ('id', 'created_on', 'priority'):
        if key in fields and (type(fields[key]) is not type(row.get(key)) or fields[key] != row.get(key)):
            raise ValueError('canonical inventory changed; capture a new manifest')
