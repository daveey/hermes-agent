"""Process reconciliation discovery for loaded local subscription owners."""
from contextlib import closing
import logging
import sqlite3

log = logging.getLogger(__name__)


def capture(actions):
    """One board inventory per profile pass, then bounded IN reads for loaded keys.

    Claims remain in the canonical action executor. No retained SQLite readers
    and no per-chat board census or board-signature scan.

    Returns ``(boards_by_action, failed)``. Each board is read in isolation: an
    unreadable board (locked, corrupt, missing) is recorded in ``failed``
    (``{slug: reason}``) and contributes nothing, while every healthy board is
    still discovered. A failed board's partial rows are discarded, never merged.
    """
    from hermes_cli import kanban_db
    from tui_gateway import server
    result = {action: [] for action in actions}
    failed = {}
    keys = {}
    for action in actions:
        key = action.session.get('session_key')
        if key:
            for chat in (key, *server._kb_stray_delegate_keys(action.session, key)):
                keys.setdefault(chat, []).append(action)
    if not keys:
        return result, failed
    boards = {}
    for meta in kanban_db.list_boards(include_archived=False):
        slug, path = server._kb_board_key(kanban_db, meta)
        boards.setdefault(path, slug)
    names = tuple(keys)
    for path, slug in boards.items():
        try:
            found = set()
            with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=0.5)) as conn:
                conn.execute('BEGIN')
                for offset in range(0, len(names), 500):
                    chunk = names[offset:offset + 500]
                    marks = ','.join('?' for _ in chunk)
                    rows = conn.execute(
                        f"SELECT DISTINCT chat_id FROM kanban_notify_subs WHERE platform='tui' AND chat_id IN ({marks})",
                        chunk)
                    found.update(chat for (chat,) in rows)
        except sqlite3.Error as exc:
            log.warning('local notification discovery skipped board %s: %s', slug, exc)
            failed[slug] = str(exc)
            continue
        for chat in found:
            for action in keys[chat]:
                if slug not in result[action]:
                    result[action].append(slug)
    return result, failed
