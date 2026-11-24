"""Owning-chat stop-only control for an ENDED card on a local-executing shadow board. [daveey]

``kanban_retain`` stops only the caller's own admitted run and refuses a shadow board
(``retain requires owning primary or genuinely local board``); ``kanban_protect_stopped``
needs a provider-refusal source and an admitted administrative requester. Neither can stop a
private shadow-board card whose run already ended (paintbot-tools M5). This module is that
control, and nothing more:

- stop only: the card goes to ``triage`` with a genuine ``administrative_stop_restricted``
  event; no run, lease, decision/spend/failure/safety episode is created; nothing is released;
- local shadow only: the receipt says ``LOCAL-SHADOW-ONLY`` / ``canonical_protected=false``.
  The event reaches the Board through the board's ordinary ordered outbox, and only after the
  Board advertises ``admin_stop_ingress: 1`` (hermes-hub ``admin_stop_ingress.ts``), whose
  barrier refuses every lease and every foreign write to the held card;
- owner only: the caller must be the card's current owning interactive chat on this gateway,
  proven by a live stop-only token (:mod:`hermes_cli.kanban_stop_context`) plus an exact
  unreleased ``created`` chat link; identity arguments, environment, machine, cron, Kanban
  worker and delegated contexts are refused before any write;
- snapshot/apply CAS: without ``expected_digest`` the call is a pure read; applying needs the
  digest of exactly that snapshot, so a concurrent claim, run, link or event refuses it.

Persistent SQL fences (:mod:`hermes_cli.kanban_stop_guards`) keep the stopped card, its runs,
events and dependency edges frozen for every later writer on this file.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync

KIND = 'administrative_stop_restricted'
STOPPABLE = ('scheduled', 'todo', 'ready', 'triage', 'blocked', 'review')
CONFLICTING = ('decision_protected_stopped', KIND)
MAX_DEADLINE = 14 * 86400


class StopRefused(ValueError):
    """A typed refusal; nothing was written."""

    def __init__(self, code: str, why: str):
        self.code = code
        super().__init__(f'{code}: {why}')


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, ensure_ascii=False)


def _mode_guard(conn):
    """Registered AND durable shadow; primary/off keep their own paths (``kanban_retain``)."""
    state = sync.sync_state(conn)
    if sync.board_mode(conn) != 'shadow' or not state or state.get('mode') != 'shadow':
        raise StopRefused('stop_ended_requires_shadow', 'only a local-executing shadow board uses this control')
    if not state.get('gateway_id') or not state.get('board_id'):
        raise StopRefused('stop_ended_identity_unknown', 'shadow board has no enrolled board/gateway identity')
    return state


def _rows(conn, sql, args):
    return [dict(r) for r in conn.execute(sql, args)]


def snapshot(conn, task_id: str) -> dict:
    """Canonical read-only state the CAS digest covers. ``tasks`` has no ``updated_at``; never use it."""
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task is None:
        raise StopRefused('stop_ended_task_missing', 'exact local task does not exist')
    task = dict(task)
    ended = conn.execute('SELECT uid,status,outcome,ended_at FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL '
                         'ORDER BY ended_at DESC, id DESC LIMIT 1', (task_id,)).fetchone()
    open_runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()[0]
    links = {'incoming': sorted(r['parent_id'] for r in conn.execute('SELECT parent_id FROM task_links WHERE child_id=?', (task_id,))),
             'outgoing': sorted(r['child_id'] for r in conn.execute('SELECT child_id FROM task_links WHERE parent_id=?', (task_id,)))}
    restrictions = sorted(r['uid'] or str(r['id']) for r in conn.execute(
        'SELECT id,uid FROM task_events WHERE task_id=? AND kind IN '
        "('decision_hold_restricted','spend_budget_triaged','decision_protected_stopped',?)", (task_id, KIND)))
    return {
        'version': 1, 'task_id': task_id, 'status': task['status'], 'current_run_id': task['current_run_id'],
        'claim_lock': task.get('claim_lock'), 'claim_expires': task.get('claim_expires'),
        'worker_pid': task.get('worker_pid'), 'created_on': task.get('created_on'),
        'ended_run': dict(ended) if ended else None, 'open_runs': open_runs,
        'max_event_id': conn.execute('SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0],
        'links': links, 'restrictions': restrictions,
    }


def digest(snap: dict) -> str:
    return hashlib.sha256(canonical(snap).encode()).hexdigest()


def _eligible(conn, state, snap):
    if snap['created_on'] not in (None, '', state['gateway_id']):
        raise StopRefused('stop_ended_foreign_card', 'card lives on another gateway; its home must stop it')
    from hermes_cli.kanban_foreign_card import check as assert_local_card
    assert_local_card(conn, snap['task_id'])
    if (snap['status'] not in STOPPABLE or snap['current_run_id'] is not None or snap['claim_lock']
            or snap['claim_expires'] or snap['worker_pid'] or snap['open_runs']):
        raise StopRefused('stop_ended_target_active', 'target must be ended, unclaimed and non-terminal')
    if not snap['ended_run'] or not snap['ended_run'].get('uid'):
        raise StopRefused('stop_ended_no_ended_run', 'target has no ended run with a portable UID')
    if conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind IN (?,?) LIMIT 1',
                    (snap['task_id'], *CONFLICTING)).fetchone():
        raise StopRefused('stop_ended_conflicting_hold', 'existing stop custody is kept, never merged or replaced')
    from hermes_cli.kanban_containment_guard import pending
    if pending(conn, snap['task_id']):
        raise StopRefused('stop_ended_conflicting_hold', 'pending containment transport owns this card')


def normalize(reason, evidence, escalate_at, now=None) -> dict:
    now = int(time.time()) if now is None else now
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise StopRefused('stop_ended_invalid', 'reason must be 1..1000 characters')
    if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 8
            or any(not isinstance(e, str) or not e.strip() or len(e) > 512 for e in evidence)):
        raise StopRefused('stop_ended_invalid', 'evidence must be 1..8 strings of at most 512 characters')
    if type(escalate_at) is not int or not now < escalate_at <= now + MAX_DEADLINE:
        raise StopRefused('stop_ended_invalid', 'escalate_at must be a fixed future Unix time within 14 days')
    return {'reason': reason, 'evidence': list(evidence), 'escalate_at': escalate_at}


def request_uid(board: str, gateway: str, task_id: str, ended_run_uid: str) -> str:
    return 'as-' + hashlib.sha256(canonical([board, gateway, task_id, ended_run_uid]).encode()).hexdigest()


def _stored(conn, task_id):
    row = conn.execute('SELECT id,uid,payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id LIMIT 1',
                       (task_id, KIND)).fetchone()
    return (row, json.loads(row['payload'])) if row else (None, None)


def read(conn, *, board: str, task_id: str) -> dict:
    """Pure read: snapshot + digest + any stored receipt. Writes nothing, reserves nothing."""
    state = _mode_guard(conn)
    snap = snapshot(conn, task_id)
    row, payload = _stored(conn, task_id)
    if row is not None:
        from hermes_cli.kanban_wake_check import get_state
        return {'ok': True, 'mode': 'readback', 'receipt': payload['receipt'], 'event_uid': row['uid'],
                'status': snap['status'], 'execution_restricted': True,
                'clock_wake_eligible': snap['status'] == 'scheduled',
                'wake_check_registered': get_state(conn, task_id) is not None}
    _eligible(conn, state, snap)
    return {'ok': True, 'mode': 'snapshot', 'board': board, 'gateway': state['gateway_id'], 'task_id': task_id,
            'expected_digest': digest(snap), 'snapshot': snap, 'writes': 0}


def apply(conn, *, board: str, task_id: str, expected_digest: str, owner, capability, reason, evidence, escalate_at) -> dict:
    """CAS-apply the stop. ``owner`` is a live :class:`kanban_stop_context.OwnerToken`; ``capability`` reads the
    Board's ``admin_stop_ingress`` flag. Lock order: token lock, then BEGIN IMMEDIATE, never the reverse."""
    frozen = normalize(reason, evidence, escalate_at)
    if not isinstance(expected_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
        raise StopRefused('stop_ended_invalid', 'expected_digest must be the 64-hex snapshot digest')
    state = _mode_guard(conn)
    if state['board_id'] != board:
        raise StopRefused('stop_ended_identity_unknown', 'board does not match the enrolled shadow board')
    with owner.held() as who:
        stored_row, stored = _stored(conn, task_id)
        if stored is None and capability(board) != 1:
            # Unknown/old Board: a published row would not be a hold there. Refuse, never claim coverage.
            raise StopRefused('stop_ended_ingress_unsupported', 'Board does not advertise admin_stop_ingress: 1')
        with kb.write_txn(conn):
            owner.verify_link(conn, task_id, who)
            stored_row, stored = _stored(conn, task_id)
            if stored is not None:
                request = stored['request']
                if (request['expected_digest'], request['frozen'], request['owner']) != (expected_digest, frozen, who):
                    raise StopRefused('stop_ended_conflict', 'card already stopped by a different frozen request')
                return {**stored['receipt'], 'event_uid': stored_row['uid'], 'replayed': True}
            snap = snapshot(conn, task_id)
            _eligible(conn, state, snap)
            if digest(snap) != expected_digest:
                raise StopRefused('stop_ended_target_changed', 'snapshot changed since the digest was read')
            uid = request_uid(board, state['gateway_id'], task_id, snap['ended_run']['uid'])
            receipt = {
                'version': 1, 'ok': True, 'request_uid': uid, 'board': board, 'gateway': state['gateway_id'],
                'task_id': task_id, 'ended_run_uid': snap['ended_run']['uid'], 'digest': expected_digest,
                'protection_scope': 'LOCAL-SHADOW-ONLY', 'canonical_protected': False,
                'dependency_satisfied': False, 'incomplete': True, 'terminal_for_run': False,
                'execution_authority': False, 'status': 'triage', 'escalate_at': frozen['escalate_at'],
            }
            kb._append_event(conn, task_id, KIND, {
                'version': 1, 'protection_scope': 'LOCAL-SHADOW-ONLY', 'canonical_protected': False,
                'dependency_satisfied': False, 'incomplete': True, 'terminal_for_run': False,
                'request': {'request_uid': uid, 'expected_digest': expected_digest, 'frozen': frozen, 'owner': who},
                'receipt': receipt})
            # One statement: with fences already installed by an earlier stop, the event above makes this
            # card fenced, and a leftover worker_started_at (run endings keep it) would refuse the update.
            from hermes_cli.kanban_stop_guards import _columns, install
            token = ',worker_started_at=NULL' if 'worker_started_at' in _columns(conn, 'tasks') else ''
            conn.execute(f"UPDATE tasks SET status='triage'{token} WHERE id=?", (task_id,))
            # The clock selector only considers scheduled cards. Remove its probe now too,
            # without deleting the historical parking event or changing any dependency.
            from hermes_cli.kanban_wake_check import set_check
            set_check(conn, task_id, None, None)
            install(conn)
            event_uid = conn.execute('SELECT uid FROM task_events WHERE task_id=? AND kind=?', (task_id, KIND)).fetchone()[0]
            return {**receipt, 'event_uid': event_uid, 'replayed': False}


def stopped_at(conn, task_id: str):
    """Execution restriction time (feeds ``restricted_since``, never the accounting floor)."""
    try:
        row = conn.execute('SELECT MIN(COALESCE(created_at,0)) FROM task_events WHERE task_id=? AND kind=?',
                           (task_id, KIND)).fetchone()
    except sqlite3.OperationalError:  # schema-less connection; a real board always has task_events
        return None
    return row[0] if row else None
