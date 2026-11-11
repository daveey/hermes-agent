"""[daveey] A managed Kanban turn checkpointed at its owner's restart continues in the next owner.

The old owner books a clean restart Stop as a checkpoint (``gateway.session_kanban_restart``: a
``restart_checkpoint`` event naming the stopped admission, and a ``continuable`` owner-restart
``worker_result``). The run's submitter (``hermes_cli.kanban_worker_client``) keeps its claim and pid,
waits for the next owner and asks ``kanban.run`` for the same attempt again. That owner restores the
attempt's session and, finding a checkpoint newer than the attempt's last admission, submits ONE
continuation admission to the same session (request id ``<attempt request>#continue:<checkpoint>``, so a
retried ask returns the same receipt) and records it as the run's newest ``owner_admitted``. The
continued turn's outcome is the attempt's outcome: same run id, claim, submitter and session.

Before the continuation, each tool call the stop left without a result gets
``[interrupted by Hermes update at <UTC>: re-run if still needed]`` (the Desktop update pause's text).

Continuation is refused (``kanban_continuation_unsafe``; the submitter then exits with the uncounted
owner-restart booking, and the next run copies this transcript, ``gateway.session_kanban_resume``)
when it cannot be proven safe: the checkpoint names another session or admission, the stopped admission is
not settled ``interrupted``, the session is unreadable, empty or rotated by compaction, or the attempt was
superseded (task/run/claim moved). Each refusal is logged and booked as ``restart_continue_refused``.
"""
from __future__ import annotations

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import NoReturn

from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)

UNSAFE = 'kanban_continuation_unsafe'
CONTINUE_PREFIX = '[Kanban run continued after a gateway restart]'
CONTINUE_TEXT = (CONTINUE_PREFIX + ' The gateway restarted (a Hermes update) while this turn was running; '
                 'this is the same run, claim and session. Continue the task from where the conversation above '
                 'stops. A tool call the restart interrupted may or may not have taken effect: check the current '
                 'state before repeating any side effect.')
INTERRUPTED_RESULT = '[interrupted by Hermes update at {at}: re-run if still needed]'
CONTINUED_EVENT = 'restart_continued'
REFUSED_EVENT = 'restart_continue_refused'
# One continuation prepare per session at a time: a retried ``kanban.run`` racing the first must not
# append a second synthetic result for the same interrupted tool call (prepare runs off the owner loop).
_PREPARING: dict[str, threading.Lock] = {}
_PREPARING_GUARD = threading.Lock()


def interrupted_result(at) -> str:
    """Card A's mid-tool cancel text, at the checkpoint's UTC time (ISO-8601, seconds, ``Z``)."""
    try:
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    return INTERRUPTED_RESULT.format(at=stamp)


def is_continuation(text) -> bool:
    return isinstance(text, str) and text.startswith(CONTINUE_PREFIX)


def _latest(conn, task_id, run_id, kind):
    row = conn.execute('SELECT id, payload FROM task_events WHERE task_id=? AND run_id=? AND kind=? '
                       'ORDER BY id DESC LIMIT 1', (task_id, run_id, kind)).fetchone()
    if row is None:
        return None, {}
    try:
        payload = json.loads(row[1] or '{}')
    except (TypeError, ValueError):
        payload = {}
    return int(row[0]), payload if isinstance(payload, dict) else {}


def continuation_request_id(base_request_id: str, token: str) -> str:
    return f'{base_request_id}#continue:{token}'


def pending_admission(db_path, params, base_request_id):
    """``(request_id, checkpoint)`` this ask must submit: the attempt's newest admission (``checkpoint`` None:
    a retried ask, same receipt), or a new continuation of the checkpoint newer than it. Read-only."""
    uri = Path(db_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
        admitted_id, admitted = _latest(conn, params['task_id'], params['run_id'], 'owner_admitted')
        checkpoint_id, checkpoint = _latest(conn, params['task_id'], params['run_id'], 'restart_checkpoint')
    current = admitted.get('request_id') if isinstance(admitted.get('request_id'), str) else None
    if current is None or not (current == base_request_id or current.startswith(base_request_id + '#continue:')):
        current = base_request_id
    if checkpoint_id is None or admitted_id is None or checkpoint_id < admitted_id:
        return current, None
    if checkpoint.get('request_id') != current or not isinstance(checkpoint.get('checkpoint'), str):
        return current, {'unsafe': 'checkpoint_admission_mismatch', **checkpoint}
    return continuation_request_id(base_request_id, checkpoint['checkpoint']), checkpoint


def _refuse(context, checkpoint, reason) -> NoReturn:
    logger.warning('Kanban restart continuation refused for %s run %s: %s; the run is requeued uncounted '
                   'and its next run resumes this transcript', context.get('task_id'), context.get('run_id'), reason)
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
            kb._append_event(conn, context['task_id'], REFUSED_EVENT,
                             {'reason': reason, 'checkpoint': (checkpoint or {}).get('checkpoint')},
                             run_id=context['run_id'])
    except Exception:  # health: allow BLE001 -- the refusal itself must reach the submitter
        logger.warning('Kanban restart continuation: recording the refusal failed', exc_info=True)
    raise RuntimeStoreError(UNSAFE)


def _unanswered(history):
    from gateway.session_kanban_resume import _unanswered_tool_calls
    return _unanswered_tool_calls(history)


def _answer_interrupted_calls(session_db, session_id, at) -> int:
    """Append the interrupted-by-update result for each trailing tool call left without one."""
    history = session_db.get_messages_as_conversation(session_id)
    calls = _unanswered(history)
    if not calls:
        return 0
    text = interrupted_result(at)
    from agent.message_metadata import TOOL_CALL_UID, tool_call_uid_from_history
    rows = []
    for call in calls:
        msg = {'role': 'tool', 'tool_call_id': call['id'], 'content': text}
        if call.get('name'):
            msg['tool_name'] = call['name']
        uid = tool_call_uid_from_history([*history, *rows, msg], len(history) + len(rows), {})
        if uid:
            msg[TOOL_CALL_UID] = uid
        rows.append(msg)

    def write(conn):
        added, _ = session_db._insert_message_rows(conn, session_id, rows)
        conn.execute('UPDATE sessions SET message_count=message_count+? WHERE id=?', (added, session_id))
        return added
    return int(session_db._execute_write(write))


def prepare(authority, *, session_id, **kwargs) -> dict:
    """Prove the checkpointed turn can continue in ``session_id``, answer its interrupted tool calls and
    record the continuation as the run's newest ``owner_admitted``. Off the owner loop; raises
    ``RuntimeStoreError(UNSAFE)`` (booked, logged) when continuation cannot be proven safe."""
    with _PREPARING_GUARD:
        lock = _PREPARING.setdefault(session_id, threading.Lock())
    with lock:
        return _prepare(authority, session_id=session_id, **kwargs)


def _prepare(authority, *, principal_id, session_id, context, checkpoint, request_id, owner_db) -> dict:
    if checkpoint.get('unsafe'):
        _refuse(context, checkpoint, checkpoint['unsafe'])
    if checkpoint.get('session_id') != session_id:
        _refuse(context, checkpoint, 'session_mismatch')
    db = authority.db
    from hermes_state_runtime import find_session_admission
    try:
        prior = find_session_admission(db, principal_id=principal_id, session_id=session_id,
                                       request_id=checkpoint.get('request_id') or '')
    except (sqlite3.Error, OSError, ValueError):
        prior = None
    if not prior or prior.get('status') != 'terminal' or prior.get('outcome') != 'interrupted' or (
            checkpoint.get('admission_id') and prior.get('admission_id') != checkpoint['admission_id']):
        _refuse(context, checkpoint, 'admission_not_interrupted')
    try:
        session = db.get_session(session_id)
        history = db.get_messages_as_conversation(session_id)
    except Exception:  # health: allow BLE001 -- an unreadable transcript is a fallback, never a crash
        logger.warning('Kanban restart continuation: session %s unreadable', session_id, exc_info=True)
        session, history = None, None
    if session is None or history is None:
        _refuse(context, checkpoint, 'session_unreadable')
    if (session.get('end_reason') if isinstance(session, dict) else None) == 'compression':
        _refuse(context, checkpoint, 'session_rotated')
    if not history:
        _refuse(context, checkpoint, 'session_empty')
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    import psutil
    from hermes_cli.process_identity import worker_birth

    def current(conn):
        """``(admitted payload, task)`` when this attempt still owns the run and ``checkpoint`` is its newest."""
        task = kb.get_task(conn, context['task_id'])
        _, latest_checkpoint = _latest(conn, context['task_id'], context['run_id'], 'restart_checkpoint')
        _, admitted = _latest(conn, context['task_id'], context['run_id'], 'owner_admitted')
        if not (task and task.status == 'running' and task.current_run_id == context['run_id']
                and task.claim_lock == context['claim_lock']
                and latest_checkpoint.get('checkpoint') == checkpoint.get('checkpoint')
                and admitted.get('request_id') in (request_id, checkpoint.get('request_id'))):
            return None, None
        return admitted, task
    # Read-only first: a superseded attempt never gets a synthetic tool result in its session.
    with connect_closing(Path(context['db'])) as conn:
        if current(conn)[1] is None:
            _refuse(context, checkpoint, 'attempt_superseded')
    answered = _answer_interrupted_calls(db, session_id, checkpoint.get('at'))
    with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
        admitted, task = current(conn)
        if task is not None and (admitted or {}).get('request_id') != request_id:
            kb._append_event(conn, task.id, 'owner_admitted', {
                'db': owner_db, 'session_id': session_id, 'request_id': request_id, 'pid': os.getpid(),
                'birth': worker_birth(psutil.Process()), 'continues': checkpoint.get('request_id'),
                'checkpoint': checkpoint.get('checkpoint')}, run_id=task.current_run_id)
            kb._append_event(conn, task.id, CONTINUED_EVENT, {
                'checkpoint': checkpoint.get('checkpoint'), 'session_id': session_id,
                'request_id': request_id, 'prior_admission_id': prior.get('admission_id'),
                'interrupted_calls': answered,
                'paused_seconds': max(0, int(time.time()) - int(checkpoint.get('at') or time.time()))},
                run_id=task.current_run_id)
    if task is None:
        _refuse(context, checkpoint, 'attempt_superseded')
    logger.info('Kanban restart: continuing %s run %s in session %s (%d interrupted tool call(s) answered)',
                context['task_id'], context['run_id'], session_id, answered)
    return {'request_id': request_id, 'answered': answered}
