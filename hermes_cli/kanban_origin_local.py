"""Read-only, binding-qualified evidence from the current profile's owner board."""
from contextlib import closing
import json
import sqlite3


def observe_local_binding(binding: dict, *, profile: str, now: float, home=None) -> dict | None:
    """Never infer an owner from an ID or release a receipt without its marker.

    Caller has already matched the trusted local gateway and profile. Shared or
    overridden board locations are ambiguous and cannot authorize this overlay.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin import _ro
    from hermes_cli.kanban_origin_ledger import observe_owner_task
    from hermes_cli.profiles import get_active_profile_name

    from pathlib import Path
    owner = binding['owner']
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
            return observe_owner_task(conn, task, now=now, local_only=explicit)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
