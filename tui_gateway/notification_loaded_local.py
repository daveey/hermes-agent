"""Process reconciliation discovery for loaded local subscription owners."""
from contextlib import closing
import sqlite3


def capture(actions):
    """One board inventory per profile pass, then bounded IN reads for loaded keys.

    Claims remain in the canonical action executor. No retained SQLite readers
    and no per-chat board census or board-signature scan.
    """
    from hermes_cli import kanban_db
    from tui_gateway import server
    result = {action: [] for action in actions}
    keys = {}
    for action in actions:
        key = action.session.get('session_key')
        if key:
            for chat in (key, *server._kb_stray_delegate_keys(action.session, key)):
                keys.setdefault(chat, []).append(action)
    if not keys:
        return result
    boards = {}
    for meta in kanban_db.list_boards(include_archived=False):
        slug, path = server._kb_board_key(kanban_db, meta)
        boards.setdefault(path, slug)
    names = tuple(keys)
    for path, slug in boards.items():
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=0.5)) as conn:
            conn.execute('BEGIN')
            for offset in range(0, len(names), 500):
                chunk = names[offset:offset + 500]
                marks = ','.join('?' for _ in chunk)
                rows = conn.execute(
                    f"SELECT DISTINCT chat_id FROM kanban_notify_subs WHERE platform='tui' AND chat_id IN ({marks})",
                    chunk)
                for (chat,) in rows:
                    for action in keys[chat]:
                        if slug not in result[action]:
                            result[action].append(slug)
    return result
