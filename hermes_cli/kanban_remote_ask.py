"""Owner-snapshot ask envelopes for origin chats on another gateway."""
import json
import math


def publish(conn, task: dict) -> dict | None:
    """Publish only the latest current park's ask, never a historical question."""
    status = task.get('status')
    if status not in ('scheduled', 'blocked'):
        return None
    kinds = ('scheduled', 'continued', 'decision_requested') if status == 'scheduled' else ('blocked',)
    marks = ','.join('?' for _ in kinds)
    row = conn.execute(
        f'SELECT id,kind,created_at,payload FROM task_events WHERE task_id=? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1',
        (task['id'], *kinds)).fetchone()
    if row is None or (status == 'scheduled' and row[1] != 'decision_requested'):
        return None
    return {'event_id': row[0], 'kind': row[1], 'created_at': row[2], 'payload': json.loads(row[3] or '{}')}


def envelope(task: dict) -> dict | None:
    """Read only a well-formed ask matching the card's current state."""
    notice = task.get('ask_notice')
    if not isinstance(notice, dict):
        return None
    expected = {'scheduled': 'decision_requested', 'blocked': 'blocked'}.get(task.get('status'))
    if (expected is None or notice.get('kind') != expected or type(notice.get('event_id')) is not int
            or notice['event_id'] <= 0 or type(notice.get('created_at')) not in (int, float)
            or not math.isfinite(notice['created_at']) or not isinstance(notice.get('payload'), dict)):
        return None
    return notice


def own_comment_wait(cmd: str | None, task_id: str) -> bool:
    """A wait on replies to this card is a chat ask, not an external producer."""
    import os
    import shlex
    try:
        words = shlex.split(cmd or '')
    except ValueError:
        return False
    return any(os.path.basename(words[i]) == 'hermes' and words[i + 1:i + 4] == ['kanban', 'wait-comment', task_id]
               for i in range(len(words) - 3))


def notifiable(task: dict) -> bool:
    return task.get('status') in ('done', 'blocked') or envelope(task) is not None
