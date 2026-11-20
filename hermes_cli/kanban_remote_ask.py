"""Owner-snapshot ask envelopes for origin chats on another gateway."""
import json
import math


def publish(conn, task: dict) -> dict | None:
    """Publish only the latest current park's ask, never a historical question."""
    status = task.get('status')
    # A blocked card already reaches the origin chat through the terminal notice, whose identity must not change
    # on rollout (re-notifying delivered blocks / resurrecting stale ones): only a parked ask is enveloped.
    if status != 'scheduled':
        return None
    kinds = ('scheduled', 'continued', 'decision_requested')
    marks = ','.join('?' for _ in kinds)
    row = conn.execute(
        f'SELECT id,kind,created_at,payload FROM task_events WHERE task_id=? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1',
        (task['id'], *kinds)).fetchone()
    if row is None or row[1] != 'decision_requested':
        return None
    return {'event_id': row[0], 'kind': row[1], 'created_at': row[2], 'payload': json.loads(row[3] or '{}')}


def envelope(task: dict) -> dict | None:
    """Read only a well-formed ask matching the card's current state."""
    notice = task.get('ask_notice')
    if not isinstance(notice, dict):
        return None
    expected = 'decision_requested' if task.get('status') == 'scheduled' else None
    if (expected is None or notice.get('kind') != expected or type(notice.get('event_id')) is not int
            or notice['event_id'] <= 0 or type(notice.get('created_at')) not in (int, float)
            or not math.isfinite(notice['created_at']) or not isinstance(notice.get('payload'), dict)):
        return None
    return notice


def _named_comment_producer(args: list[str]) -> bool:
    """``--from-gateway`` / ``--from-card`` already names a producer other than the creator chat."""
    i = 0
    while i < len(args):
        word = args[i]
        for flag in ('--from-gateway', '--from-card'):
            if word == flag:
                return bool(i + 1 < len(args) and not str(args[i + 1]).startswith('-') and str(args[i + 1]).strip())
            if word.startswith(flag + '='):
                return bool(word.split('=', 1)[1].strip())
        i += 1
    return False


def own_comment_wait(cmd: str | None, task_id: str) -> bool:
    """A wait on replies to this card is a chat ask, not an external producer."""
    import os
    import shlex
    try:
        words = shlex.split(cmd or '')
    except ValueError:
        return False
    for i in range(len(words) - 3):
        if os.path.basename(words[i]) == 'hermes' and words[i + 1:i + 4] == ['kanban', 'wait-comment', task_id]:
            if not _named_comment_producer(words[i + 4:]):
                return True
    return False


def notifiable(task: dict) -> bool:
    return task.get('status') in ('done', 'blocked') or envelope(task) is not None
