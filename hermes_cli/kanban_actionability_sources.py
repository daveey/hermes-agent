"""Read-only SQLite fact adapters for actionability. No migrations, refresh or side effects. [daveey]"""
from __future__ import annotations

import json
import sqlite3
import time

from hermes_cli.kanban_actionability import owner, positive_time, project, valid_awaiting
from hermes_cli.kanban_actionability_evidence import EVIDENCE_ERRORS, effective
from hermes_cli.kanban_actionability_selection import KINDS as SOURCE_KINDS, LANDINGS, select

KINDS = tuple(dict.fromkeys((*('created', 'blocked', 'dependency_wait', 'scheduled', 'continued', 'spend_budget_triaged',
         'decision_hold_restricted', 'decision_retained', 'decision_protected_stopped',
         'spend_budget_decision_ready', 'promoted_manual', 'archived', 'contained'),
         *(kind for kinds in SOURCE_KINDS.values() for kind in kinds))))


def record(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or '{}')
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _human_step(raw) -> str | None:
    request = record(raw)
    required = ('action', 'why_human', 'scope', 'evidence')
    if not all(isinstance(request.get(k), str) and request[k].strip() for k in required):
        return None
    return request['action'] if request.get('interaction') in ('approval', 'reply', 'user_terminal', 'physical', 'external') else None


class Reader:
    """Only fixed local schema names and parameterized coordinates; optional legacy tables are unknown."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # Fresh snapshot reads, not a schema-cookie cache: aggregate inside SQLite
        # rather than hand the GIL back once per table/column on every card.
        if getattr(conn, 'text_factory', None) is str:
            self.tables = set(json.loads(conn.execute(
                "SELECT json_group_array(name) FROM sqlite_master WHERE type='table'").fetchone()[0]))
        else:
            self.tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.schema = {}
        self.event_columns = self.columns('task_events')
        self.events_complete = {'task_id', 'kind', 'payload', 'created_at', 'id'} <= self.event_columns
        self.partial = not self.events_complete
        self.malformed = set()
        sync_columns = self.columns('kanban_sync_state')
        self.synced = 'mode' in sync_columns and bool(conn.execute(
            "SELECT 1 FROM kanban_sync_state WHERE mode!='off' LIMIT 1").fetchone())
        self.partial |= 'kanban_sync_state' in self.tables and 'mode' not in sync_columns
        shapes = {'task_runs': {'id', 'task_id', 'status', 'ended_at'},
                  'task_wake_checks': {'task_id', 'cmd'},
                  'kanban_failure_commands': {'task_id', 'run_uid', 'request_uid', 'operation_kind',
                                             'delivery_state', 'canonical_receipt_json'}}
        self.partial |= any(not required <= self.columns(table) for table, required in shapes.items())

    def columns(self, table: str) -> set[str]:
        if table not in self.schema:
            if getattr(self.conn, 'text_factory', None) is str:
                from hermes_cli.kanban_table_columns import table_columns
                self.schema[table] = set(table_columns(self.conn, table))
            else:
                self.schema[table] = {r[0] for r in self.conn.execute('SELECT name FROM pragma_table_info(?)', (table,))}
        return self.schema[table]

    def rows(self, sql: str, params=()) -> list[dict]:
        cursor = self.conn.execute(sql, params)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor]

    def task(self, tid: str) -> dict | None:
        rows = self.rows('SELECT * FROM tasks WHERE id=?', (tid,))
        return rows[0] if rows else None

    def events(self, tid: str) -> list[dict]:
        if not self.events_complete:
            return []
        # UUIDv7 random bits do not order two appends in one millisecond.
        # Local insertion order is diagnostic only; canonical issuance uses inventory ord.
        order = 'created_at DESC,id DESC'
        marks = ','.join('?' for _ in KINDS)
        live = ' AND deleted_at IS NULL' if 'deleted_at' in self.event_columns else ''
        rows = self.rows(f'SELECT * FROM task_events WHERE task_id=? AND kind IN ({marks}){live} ORDER BY {order}',
                         (tid, *KINDS))
        for row in rows:
            try:
                payload = json.loads(row['payload'] or '{}')
                if not isinstance(payload, dict) or any(
                        payload.get(k) is not None and not isinstance(payload[k], str)
                        for k in ('episode', 'episode_uid', 'cause')) or any(
                        payload.get(k) is not None and not isinstance(payload[k], dict)
                        for k in ('next_action', 'producer')) or not valid_awaiting(payload.get('awaiting')):
                    self.malformed.add(tid)
            except (ValueError, TypeError):
                self.malformed.add(tid)
        return [{**r, 'data': record(r['payload'])} for r in rows]

    def uid(self, event: dict) -> str | None:
        if 'uid' in self.event_columns and event.get('uid'):
            return event['uid']
        return None if self.synced else str(event['id'])

    def pending(self, tid: str) -> dict | None:
        if 'kanban_failure_commands' not in self.tables:
            return None
        columns = self.columns('kanban_failure_commands')
        if not {'task_id', 'run_uid', 'request_uid', 'operation_kind', 'delivery_state', 'canonical_receipt_json'} <= columns:
            return None
        rows = self.rows("SELECT * FROM kanban_failure_commands WHERE task_id=? AND operation_kind='retain' "
                         "AND (delivery_state IS NULL OR delivery_state NOT IN ('confirmed','rejected')) "
                         "ORDER BY run_uid,request_uid LIMIT 2", (tid,))
        return {**rows[0], '_ambiguous': len(rows) > 1} if rows else None


def _release_offer(conn, tid: str) -> dict | None:
    """What `hermes kanban release` would act on for ``tid``, else None. Read-only. Mirrors kanban_release._eligible:
    offer() (held status, pending stop, safety restriction) plus the same containment guard release refuses on,
    which also reads `contained` events and sync quarantine, not only tasks.containment_episode."""
    try:
        from hermes_cli import kanban_release
        from hermes_cli.kanban_containment_guard import pending as contained
        offer = kanban_release.offer(conn, tid)
        return None if offer is None or contained(conn, tid) else offer
    except EVIDENCE_ERRORS:
        return None


def _unresolved_receipt(events: list[dict]) -> dict | None:
    promoted = False
    for event in events:
        if event['kind'] == 'promoted_manual':
            promoted = True
        if event['kind'] not in ('decision_retained', 'decision_protected_stopped'):
            continue
        disposition = record(event['data'].get('disposition'))
        restricted = event['kind'] == 'decision_protected_stopped' or disposition.get('reason') == 'scope_stopped' or event['data'].get('superseded_by_safety_stop') is True
        if restricted or not promoted:
            return event
    return None


def _state(reader, tid, events):
    stop, restricted, unknown = effective(reader, tid)
    try:
        from hermes_cli.kanban_scope_renewal import covered, renewals
        from hermes_cli.kanban_actionability_evidence import Rows
        renewed = covered(Rows(reader.conn), tid)
        valid = renewals(Rows(reader.conn), tid)
    except EVIDENCE_ERRORS:
        renewed, valid, unknown = (), [], True
    remaining = [e for e in events if e['data'].get('superseded_by_safety_stop') is True or (
                 reader.uid(e) not in renewed and not any(
                     e['data'].get('episode_uid') == r['payload']['episode'] and
                     int(e['id']) < int(r['id']) for r in valid))]
    receipt = None if stop else _unresolved_receipt(remaining)
    return stop or receipt, restricted, reader.partial or unknown or bool(receipt) or tid in reader.malformed


def _replica_parent_terminal(reader: Reader, tid: str) -> bool:
    """Missing-live edge only: borrow lifecycle status, never replica stop/custody history."""
    from hermes_cli.kanban_shadow_view import _open_card_replica
    try:
        with _open_card_replica(reader.conn) as replica:
            if replica is None:
                return False
            row = replica.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()
            return row is not None and row[0] in ('done', 'archived')
    except EVIDENCE_ERRORS:
        return False


def _dependencies(reader: Reader, tid: str) -> tuple[list[dict], bool]:
    if 'task_links' not in reader.tables:
        return [], True
    result, unknown = [], False
    for (pid,) in reader.conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id', (tid,)):
        parent = reader.task(pid)
        if parent is None:
            if _replica_parent_terminal(reader, pid):
                continue
            result.append({'task_id': pid, 'status': None, 'owner': owner(None),
                           'trigger': f'Recover authoritative parent evidence for {pid}'})
            unknown = True
            continue
        parent_events = reader.events(pid)
        stop, restricted, partial = _state(reader, pid, parent_events)
        unknown |= partial or reader.synced
        contained = parent.get('containment_episode')
        if parent['status'] in ('done', 'archived') and not stop and not contained and not partial:
            continue
        episode = (stop['data'].get('episode_uid') or stop['data'].get('episode') or reader.uid(stop)) if stop else None
        admin, _ = _administration(reader, parent_events, episode)
        step = _human_step(parent.get('human_request'))
        result.append({'task_id': pid, 'status': parent['status'], 'owner': owner(parent.get('assignee')),
                       'restricted': restricted, 'administrative_owner': admin,
                       'evidence_state': 'unknown' if partial or reader.synced else 'known',
                       'stop_episode': (stop['data'].get('episode_uid') or stop['data'].get('episode') or reader.uid(stop)) if stop else None,
                       'containment': contained,
                       'trigger': f'{pid}: {step}' if isinstance(step, str) and step.strip() else (
                           f'{pid}: supported resolution of its protected episode' if stop or contained else
                           f'{pid}: accountable owner completes its acceptance')})
    return result, unknown


def _route_owner(reader: Reader, route: dict) -> dict:
    tid = route.get('task_id')
    if not tid and route.get('kind') == 'task' and isinstance(route.get('refs'), list):
        tid = next(iter(route['refs']), None)
    if isinstance(tid, str):
        task = reader.task(tid)
        if not task or task['status'] in ('done', 'archived'):
            return {'kind': 'unavailable', 'refs': [tid]}
        stop, restricted, unknown = _state(reader, tid, reader.events(tid))
        if unknown or reader.synced or reader.pending(tid):
            return {'kind': 'unknown', 'refs': [tid]}
        held = stop or restricted or task.get('containment_episode')
        return {'kind': 'unavailable' if held else 'task', 'refs': [tid]}
    if route.get('kind') in ('chat', 'chats'):
        refs = route.get('refs')
        # Recorded routing is provenance, not proof that a chat is live or counted.
        return {'kind': 'chat', 'refs': list(dict.fromkeys(
            ref for ref in refs if isinstance(ref, str) and ref.strip())) if isinstance(refs, list) else []}
    return {'kind': 'unavailable', 'refs': []}


def _administration(reader: Reader, events: list[dict], episode: str | None) -> tuple[dict, dict]:
    scoped = [e for e in events if episode and (e['data'].get('episode') or e['data'].get('episode_uid')) == episode]
    routes, deadlines = [], []
    for event in scoped:
        p = event['data']
        next_action = record(p.get('next_action'))
        deadline = positive_time(next_action.get('escalate_at')) or positive_time(record(next_action.get('escalation')).get('at'))
        if deadline:
            deadlines.append(deadline)
        route = p.get('producer') or next_action.get('owner')
        if isinstance(route, dict):
            routes.append(route)
    admin = _route_owner(reader, routes[0]) if routes else {'kind': 'unavailable', 'refs': []}
    contracts = [record(e['data'].get('next_action')) for e in scoped if record(e['data'].get('next_action'))]
    return admin, {**(contracts[0] if contracts else {}), 'escalate_at': min(deadlines) if deadlines else None}


def _review_dispatch() -> bool:
    """True only when the board's config was read and says reviewers are dispatched; anything else is not established."""
    from hermes_cli.config_backend import ConfigBackendUnavailable
    try:
        from hermes_cli.config import load_config
        return load_config()['kanban']['review_dispatch'] is True
    # ConfigBackendUnavailable is a SystemExit, named on purpose: a backend outage must not abort the census.
    except (Exception, ConfigBackendUnavailable):  # health: allow BLE001 -- any config read failure means dispatch is not established; the row stays flagged
        return False


def describe(conn: sqlite3.Connection, tid: str, *, now: int | None = None, reader: Reader | None = None) -> dict:
    """One task and its immediate parents only; malformed optional evidence stays UNKNOWN."""
    reader = reader or Reader(conn)
    task = reader.task(tid)
    if task is None:
        raise ValueError(f'unknown task: {tid}')
    now = int(time.time()) if now is None else now
    try:
        return _describe(conn, tid, now=now, reader=reader)
    except EVIDENCE_ERRORS:
        row = project({'task': task, 'unknown': True}, now=now)
        if task['status'] in ('done', 'archived'):
            return row
        row['violations'] = ['evidence_unavailable']
        row.update(action='repair_lifecycle_contract', instruction='Recover authoritative task evidence')
        return row


def _private_reason(conn, task, reader, *, explain=False):
    """Local-order diagnostic hold, with the execution reader's mandate-digest verification."""
    if not reader.events_complete:
        return [] if explain else None
    from hermes_cli.kanban_actionability_private import DECISIONS, PRIVATE, held
    kinds = tuple(PRIVATE | DECISIONS)
    live = ' AND deleted_at IS NULL' if 'deleted_at' in reader.event_columns else ''
    try:
        rows = reader.rows(
            f"SELECT * FROM task_events WHERE task_id=? AND kind IN ({','.join('?' * len(kinds))}){live} ORDER BY id",
            (task['id'], *kinds))
    except sqlite3.OperationalError:
        return [] if explain else None
    private = [row for row in rows if row['kind'] in PRIVATE]
    if not private or any(not reader.uid(row) for row in private):
        return [] if explain else None  # UIDless history stays UNKNOWN, never positive provenance.
    from hermes_cli.kanban_actionability_evidence import Rows
    from hermes_cli.kanban_scope_renewal import covered, key
    events = [{'uid': key(row), 'kind': row['kind'], 'ord': row['id'], 'payload': row['payload']} for row in rows]
    try:
        verified = covered(Rows(conn), task['id'])
    except EVIDENCE_ERRORS:
        verified = frozenset()  # Missing mandate evidence cannot resolve a scope hold.
    # Synced provenance is the Board-stamped envelope, never actor text in the payload. A row this gateway
    # wrote has no envelope until its Board echo applies, so only the card's home gateway (sync identity ==
    # created_on) reads its own un-echoed release; elsewhere a pulled row may also lack one and proves nothing.
    # An unsynced board's own release writer is local; foreign envelopes are not local writes.
    home = task.get('created_on') if isinstance(task.get('created_on'), str) and task.get('created_on') else None
    if reader.synced:
        from hermes_cli.kanban_db_sync import sync_state
        origins = set() if home is None else {home}
        if home is not None and (sync_state(conn) or {}).get('gateway_id') == home:
            origins.add(None)
    else:
        origins = {None, ''}
    from hermes_cli.kanban_release_application import native_release, verified_release
    releases = frozenset(key(row) for row in rows if row['kind'] == 'stop_released' and (
        verified_release(Rows(conn), row) is not None if native_release(Rows(conn), row, record(row['payload']))
        else row.get('origin_gateway') in origins))
    if explain:
        from hermes_cli.kanban_actionability_restrictions import details
        return details(task, reader, events, verified, releases)
    return held(task['status'], events, verified_cover=verified, verified_releases=releases)


def _lane_gate_at(conn, tid, reader) -> int | None:
    """Creation time of the latest lane_gated event while the lane still holds the card, else None."""
    if ('block_kind' not in reader.columns('tasks') or
            not {'task_id'} <= reader.columns('task_runs') or not reader.events_complete):
        return None
    from hermes_cli.kanban_lanes import HELD
    live = ' AND deleted_at IS NULL' if 'deleted_at' in reader.event_columns else ''
    try:
        if conn.execute(f'SELECT 1 FROM tasks WHERE id=? AND {HELD}', (tid,)).fetchone() is None:
            return None
        gate = conn.execute(f"SELECT created_at FROM task_events WHERE task_id=? AND kind='lane_gated'{live} "
                            'ORDER BY id DESC LIMIT 1', (tid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return positive_time(gate[0]) if gate else None


def _describe(conn, tid, *, now, reader):
    """Source-derived diagnostics only. Foreign/canonical freshness is explicitly UNKNOWN."""
    reader = reader or Reader(conn)
    task = reader.task(tid)
    if task is None:
        raise ValueError(f'unknown task: {tid}')
    events = reader.events(tid)
    stop, _restricted, unknown = _state(reader, tid, events)
    schedule = next((e for e in events if e['kind'] in ('scheduled', 'continued')), None)
    # Notes/notices do not replace the material source or reset its deadline.
    source = stop or select(task['status'], events,
                            order_key=lambda e: (e['created_at'], e['id']))
    if not stop and task['status'] == 'triage':
        # Decision notices are not material clocks. Preserve legacy triage
        # diagnostics; confirmed ordinary custody selects its source separately.
        source = next((e for e in events if e['kind'] not in (
            'spend_budget_decision_ready', 'decision_retained', 'decision_protected_stopped', *LANDINGS)), None)
    deps, missing = _dependencies(reader, tid)
    episode = (stop['data'].get('episode') or stop['data'].get('episode_uid') or reader.uid(stop)) if stop else None
    episode = episode if isinstance(episode, str) and episode else None
    root = next((e for e in events if reader.uid(e) == episode), stop) if episode else stop
    admin, next_action = _administration(reader, events, episode) if stop else (None, {})
    stop_data = stop['data'] if stop else {}
    cause = stop_data.get('cause')
    cause = cause if isinstance(cause, str) and cause.strip() else 'spend_budget'
    facts = {'task': task, 'stop': {**stop_data, 'cause': cause} if stop else None,
             'stop_episode': episode, 'dependencies': deps,
             'administrative_owner': admin, 'next_action': next_action,
             'source_uid': reader.uid(source) if source else None,
             'source_at': ((root if stop else source) or {}).get('created_at'),
             'source_refs': [reader.uid(source)] if source and reader.uid(source) else [],
             'deadline_at': next_action.get('escalate_at') or record(next_action.get('escalation')).get('at'),
             'containment': task.get('containment_episode'), 'pending_command': reader.pending(tid),
             'human_step': _human_step(task.get('human_request')),
             # _release_offer is release's own eligibility; unknown evidence never vetoes it. A receipt-only row
             # (no pending stop) fails it because offer() requires the pending stop release acts on.
             'release_offer': _release_offer(conn, tid) if stop else None,
             'unknown': reader.synced or reader.partial or missing or bool(unknown) or admin and admin.get('kind') == 'chat'
                        or bool(source and not reader.uid(source))
                        or bool(task.get('human_request') and not _human_step(task['human_request']))}
    if source and not any((stop, facts['containment'], facts['pending_command'])):
        from hermes_cli.kanban_actionability_creation import validated_intent
        parent_ids = [r[0] for r in conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id', (tid,))]
        # A park's clock is its own (the wake, the block's review): an edge changed during the park moves
        # no deadline, and the landing after the wake or unblock freezes the dependency wait.
        parked = task['status'] in ('scheduled', 'blocked')
        intent = validated_intent(task, source, parent_ids=None if parked else parent_ids)
        if intent:
            facts['deadline_at'] = intent['deadline_at']
        elif 'actionability_intent' in source['data']:
            facts['invalid_creation_intent'] = True
            facts['unknown'] = True
    if schedule:
        facts.update(wake_at=schedule['data'].get('wake_at'), awaiting=schedule['data'].get('awaiting'))
    if not facts.get('deadline_at'):
        reason = _private_reason(conn, task, reader)
        if reason is None:
            for dep in deps:
                parent = reader.task(dep['task_id'])
                if parent is None:
                    continue
                parent_reason = _private_reason(conn, parent, reader)
                if parent_reason:
                    reason = f"parent {dep['task_id']} {parent_reason}"
                    break
        if reason:
            facts['private_hold'] = reason
    lane_deadline = None
    if not facts.get('stop') and not facts.get('private_hold'):
        gate_at = _lane_gate_at(conn, tid, reader)
        if gate_at:
            # A lane merge links the held card under its owner: the row then names that dependency, and the
            # lane gate it is still held at keeps its own clock (a repeated merge link never renews it).
            facts['lane_hold'] = not deps
            # Fixed at the source, never renewed by a later census observation; a recorded contract wins.
            lane_deadline = gate_at + 86400
    wake_columns = reader.columns('task_wake_checks')
    if {'task_id', 'cmd'} <= wake_columns:
        probe = conn.execute('SELECT cmd FROM task_wake_checks WHERE task_id=?', (tid,)).fetchone()
        facts['probe'] = probe[0] if probe else None
    run_columns = reader.columns('task_runs')
    facts['run_evidence_known'] = {'id', 'task_id', 'status', 'ended_at'} <= run_columns and 'current_run_id' in task
    if facts['run_evidence_known'] and task.get('current_run_id') is not None:
        runs = reader.rows('SELECT * FROM task_runs WHERE id=? AND task_id=?', (task['current_run_id'], tid))
        facts['run'] = runs[0] if runs else None
    facts['unknown'] |= bool(stop and not episode)
    if task['status'] == 'review':
        facts['review_dispatch'] = _review_dispatch()
    from hermes_cli.kanban_actionability_confirmed import compose
    observed_at = int(time.time()) if now is None else now
    row = compose(reader, task, facts, project(facts, now=observed_at), now=observed_at)
    row['private_restrictions'] = _private_reason(conn, task, reader, explain=True)
    if lane_deadline and not row['escalation']['at'] and 'escalation_deadline_missing' in row['violations']:
        row['escalation'].update(at=lane_deadline, state='pending')
        row['violations'] = [v for v in row['violations'] if v != 'escalation_deadline_missing']
        if row['blocker'] and observed_at >= lane_deadline:
            row['violations'].append('escalation_overdue')
    return row
