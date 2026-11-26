"""Read-only, binding-qualified evidence from the current profile's owner board."""
from contextlib import closing
import json
import sqlite3

_READ_ERRORS = (OSError, sqlite3.Error, ValueError, TypeError)


def _owner_db(owner: dict, *, profile: str, home):
    """The owner board's database when this profile unambiguously owns it, else None."""
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_active_profile_name

    from pathlib import Path
    explicit = home is not None
    home = Path(home).resolve() if explicit else get_hermes_home().resolve()
    board = owner['board']
    from hermes_cli.kanban_sync_client import BOARD_ID_RE
    if not BOARD_ID_RE.fullmatch(board) or owner['profile'] != profile:
        return None
    expected = home / 'kanban.db' if board == 'default' else home / 'kanban/boards' / board / 'kanban.db'
    if not expected.resolve().is_relative_to(home):
        return None
    if not explicit and (profile != get_active_profile_name()
                         or kb.kanban_home().resolve() != home or kb.kanban_db_path(board).resolve() != expected):
        return None
    return expected


def observe_local_binding(binding: dict, *, profile: str, now: float, home=None) -> dict | None:
    """Never infer an owner from an ID or release a receipt without its marker.

    Caller has already matched the trusted local gateway and profile. Shared or
    overridden board locations are ambiguous and cannot authorize this overlay.
    """
    from hermes_cli.kanban_origin import _ro
    from hermes_cli.kanban_origin_ledger import observe_owner_task

    owner = binding['owner']
    expected = _owner_db(owner, profile=profile, home=home)
    if expected is None:
        return None
    try:
        with closing(_ro(expected)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN')
            row = conn.execute('SELECT * FROM tasks WHERE id=?', (owner['task_id'],)).fetchone()
            if row is None:
                return None
            markers = [json.loads(r[0]) for r in conn.execute(
                'SELECT binding_json FROM origin_creation_intents WHERE task_id=?', (owner['task_id'],))]
            marker = {k: v for k, v in binding.items() if k != 'publication_version'}
            if marker not in markers:
                return None
            task = {**dict(row), **owner, 'id': owner['task_id'], 'origin_bindings': markers}
            return observe_owner_task(conn, task, now=now, local_only=home is not None)
    except _READ_ERRORS:
        return None


_LATEST_EVENT = ("SELECT e.task_id,{fields} FROM json_each(?) AS wanted CROSS JOIN task_events AS e"
                 " ON e.id=(SELECT MAX(id) FROM task_events WHERE kind='{kind}' AND task_id=wanted.value)")


def _prefetch(conn: sqlite3.Connection, ids: list[str]) -> dict:
    """Every row ``observe_local_binding`` reads once per card, for all of ``ids``, in this snapshot. [daveey]

    Each query keeps the requested ids outermost (``CROSS JOIN``) and reaches the latest row per card with one
    index seek, the same row ``ORDER BY id DESC LIMIT 1`` returns. Markers keep rowid order, as the keyed read.
    """
    wanted = json.dumps(ids)
    tasks = {r['id']: r for r in conn.execute(
        'SELECT t.* FROM json_each(?) AS wanted CROSS JOIN tasks AS t ON t.id=wanted.value', (wanted,))}
    markers: dict[str, list[str]] = {}
    for task_id, raw in conn.execute(
            'SELECT i.task_id,i.binding_json FROM json_each(?) AS wanted CROSS JOIN origin_creation_intents AS i'
            ' ON i.task_id=wanted.value ORDER BY i.rowid', (wanted,)):
        markers.setdefault(task_id, []).append(raw)
    completed = {r[0]: tuple(r)[1:] for r in conn.execute(
        _LATEST_EVENT.format(fields='e.id,e.run_id,e.created_at,e.payload', kind='completed'), (wanted,))}
    runs = {r[0]: tuple(r)[1:] for r in conn.execute(
        'SELECT r.task_id,r.id,r.status,r.outcome,r.ended_at,r.summary FROM json_each(?) AS wanted'
        ' CROSS JOIN task_runs AS r ON r.id=(SELECT MAX(id) FROM task_runs WHERE task_id=wanted.value)', (wanted,))}
    closed = json.dumps([t for t, r in tasks.items() if r['status'] == 'archived'])
    archived, withdrawn = ({r[0]: tuple(r)[1:] for r in conn.execute(
        _LATEST_EVENT.format(fields=fields, kind=kind), (closed,))} if closed != '[]' else {}
        for fields, kind in (('e.id,e.payload', 'archived'), ('e.id,e.created_at,e.payload', 'request_withdrawn')))
    return {'tasks': tasks, 'markers': markers, 'completed': completed, 'runs': runs, 'archived': archived,
            'withdrawn': withdrawn}


def _observe_prefetched(conn, binding: dict, read: dict, *, now: float, explicit: bool) -> dict | None:
    from hermes_cli.kanban_origin_ledger import observe_owner_task

    owner = binding['owner']
    task_id = owner['task_id']
    row = read['tasks'].get(task_id)
    if row is None:
        return None
    # Decoded per binding: two obligations on one card never share a mutable marker list.
    markers = [json.loads(raw) for raw in read['markers'].get(task_id, ())]
    marker = {k: v for k, v in binding.items() if k != 'publication_version'}
    if marker not in markers:
        return None
    task = {**dict(row), **owner, 'id': task_id, 'origin_bindings': markers}
    event = read['completed'].get(task_id)
    try:
        state = tuple(row[k] for k in ('status', 'completed_at', 'current_run_id', 'goal_mode', 'completion_contract'))
    except IndexError as exc:  # a schema without one of these columns fails the keyed read the same way
        raise sqlite3.OperationalError(str(exc)) from None
    return observe_owner_task(conn, task, now=now, local_only=explicit, read={
        'row': state,
        'markers': [json.loads(raw) for raw in read['markers'].get(task_id, ())],
        'event': event, 'run': read['runs'].get(task_id),
        'archived': read['archived'].get(task_id) if row['status'] == 'archived' and event else None,
        'withdrawn': read['withdrawn'].get(task_id)})


def observe_local_bindings(bindings: list[dict], *, profile: str, now: float, home=None) -> list[dict | None]:
    """``observe_local_binding`` for many bindings: one snapshot and one batched read per owner board. [daveey]

    Hub 2026-11-26: 384 same-owner obligations cost 4,036 statements per links projection (each card its own
    snapshot and ten keyed reads), every one a GIL hand-off inside the Desktop backend. The answer for each binding
    is what the single-card read returns in that snapshot. A card whose read fails here, and every card after it,
    is read alone again, so a broken shared handle is never the reason a card reads unknown.
    """
    from hermes_cli.kanban_origin import _ro

    out: list[dict | None] = [None] * len(bindings)
    boards: dict[tuple, list[int]] = {}
    for index, binding in enumerate(bindings):
        boards.setdefault((binding['owner']['board'], binding['owner']['profile']), []).append(index)
    for indexes in boards.values():
        expected = _owner_db(bindings[indexes[0]]['owner'], profile=profile, home=home)
        if expected is None:
            continue
        alone = list(indexes)
        try:
            with closing(_ro(expected)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('BEGIN')
                read = _prefetch(conn, sorted({bindings[i]['owner']['task_id'] for i in indexes}))
                while alone:
                    try:
                        out[alone[0]] = _observe_prefetched(conn, bindings[alone[0]], read, now=now,
                                                            explicit=home is not None)
                    except (ValueError, TypeError):
                        out[alone[0]] = None
                    alone.pop(0)
        except (*_READ_ERRORS, IndexError):
            pass
        for index in alone:
            out[index] = observe_local_binding(bindings[index], profile=profile, now=now, home=home)
    return out
