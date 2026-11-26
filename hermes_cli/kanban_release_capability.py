"""Explicit historical capability selections for the guarded home release. [daveey]

Never infer supersession from a newer stop. The caller names each capability episode;
ordinary release authority, read-first, safety, scope and containment gates still apply.
"""
from __future__ import annotations


def selected(conn, task, episodes):
    """Return exact same-card capability stop rows, or reject before release writes.

    Portable UIDs on synced stores; local event keys only on unsynced stores. Returned
    row bytes are rechecked under the deciding writer reservation, not trusted across I/O.
    """
    from hermes_cli.kanban_release import _Refusal, _payload
    from hermes_cli.kanban_scope_renewal import key
    from hermes_cli.kanban_db_sync import sync_state

    if not isinstance(episodes, (list, tuple)) or len(episodes) > 32:
        raise _Refusal('invalid_capability_episode', 'name at most 32 exact capability episodes')
    if any(not isinstance(value, str) or not value for value in episodes):
        raise _Refusal('invalid_capability_episode', 'capability episodes must be nonempty exact event keys')
    wanted = list(dict.fromkeys(episodes))
    if not wanted:
        return []
    synced = sync_state(conn) is not None
    rows = {key(dict(row)): dict(row) for row in conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'", (task,))}
    out = []
    for episode in wanted:
        row = rows.get(episode)
        payload = _payload(row['payload']) if row else {}
        if (payload.get('cause') != 'capability_unavailable'
                or payload.get('superseded_by_safety_stop') is True
                or (synced and not row.get('uid'))):
            raise _Refusal('invalid_capability_episode',
                           f'{episode} is not an exact capability stop on {task}; scope and safety stops stay separate')
        out.append(row)
    return out


def anchor(current, rows):
    """Read all required evidence since the oldest selected stop, not only the newest self-stop."""
    from hermes_cli.kanban_sync_divergence import _event_order
    return min([current, *rows], key=_event_order)


def append(conn, task, rows, current, record):
    """Append one existing-format home receipt per selected historical episode in the decision transaction."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_scope_renewal import key

    for row in rows:
        episode = key(row)
        if episode != current:
            kb._append_event(conn, task, 'stop_released', {**record, 'episode': episode})


def recorded(conn, task, rows, record):
    """A no-op/resume may acknowledge selections only if that exact release already recorded them."""
    from hermes_cli.kanban_release import _Refusal, own_events
    from hermes_cli.kanban_scope_renewal import key
    payload = {name: value for name, value in record.items() if name != '_id'}
    releases = [event['payload'] for event in own_events(conn, task, ('stop_released',))]
    if any({**payload, 'episode': key(row)} not in releases for row in rows):
        raise _Refusal('capability_selection_not_released',
                       'this recorded release did not cover every selected capability episode; '
                       'no additional receipts were written. Explicit selections require a pending stopped card, '
                       'not an already released or resuming decision')


def rerun(task, rows):
    """Preserve exact selections in read-before-release retry instructions."""
    import shlex
    from hermes_cli.kanban_scope_renewal import key
    return shlex.join(['hermes', 'kanban', 'release', task,
                       *(part for row in rows for part in ('--capability-episode', key(row)))])
