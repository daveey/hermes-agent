"""Prospective, refusal-only declaration for non-prerequisite assistance cards."""
import re

# A standalone machine-readable declaration, not a title/creator heuristic.
_DECLARATION = re.compile(r'^<!-- kanban-helper-for: (t_(?:[0-9a-f]{16}|[0-9a-f]{8})) -->$', re.MULTILINE)


def check_helper_parent(conn, parent_id: str, child_id: str) -> None:
    """Refuse helper -> its declared source; never remove or waive an edge."""
    row = conn.execute('SELECT body FROM tasks WHERE id=?', (parent_id,)).fetchone()
    if row and child_id in _DECLARATION.findall(row['body'] or ''):
        raise ValueError(
            f'helper_parent_forbidden: cannot link {parent_id} -> {child_id}: '
            'the parent explicitly declares assistance for this source, not a prerequisite. '
            'Keep creator_task_id ownership without this dependency; '
            'use a separate ordinary review/landing card for genuinely required work. '
            'Existing dependencies and retained restrictions are unchanged.'
        )
