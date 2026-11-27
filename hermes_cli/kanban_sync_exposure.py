"""Dormant pending-custody capture and authenticated HTTP exposure observations.

Candidate coverage is NOT certified completeness: old-writer drain, the minimum
writer gate and the rest of the insertion/recovery audit remain prerequisites.
Historical missing history stays unknown even after a new observed transmission.
No permanent per-operation ledger or credential material is stored.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from urllib.parse import quote

from hermes_cli.kanban_db_connect import write_txn


def task_insert(uid: str, fields: str, uuid: str, clock: str) -> str:
    """New task INSERT only; unresolved predecessors cannot acquire candidates.

    A materialized expression samples identity/time once. This is part of the
    original capture INSERT, not a retrofit inferred later from row appearance.
    C has not enabled certification: identity_state deliberately remains NULL.
    """
    eligible = (f"NOT EXISTS (SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid={uid}) "
                f"AND NOT EXISTS (SELECT 1 FROM kanban_sync_tombstones WHERE table_name='tasks' AND row_uid={uid}) "
                f"AND NOT EXISTS (SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id={uid})")
    return ("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,base_version,hlc,created_at,"
            "capture_protocol,original_op_id,original_created_at,exposure_history) "
            f"WITH capture AS MATERIALIZED (SELECT {uuid} AS id,{clock} AS ms,({eligible}) AS eligible) "
            f"SELECT id,'tasks',{uid},'upsert',{fields},NULL,'',ms,"
            "CASE WHEN eligible THEN 'capture_candidate1' END,CASE WHEN eligible THEN id END,"
            "CASE WHEN eligible THEN ms END,CASE WHEN eligible THEN "
            "'{\"protocol\":\"exposure1\",\"coverage\":\"candidate\",\"certified\":false,\"exposures\":[]}' END FROM capture;")


class ExposureCustodyError(OSError):
    """A board-local refusal, caught by the sync round without losing custody."""


def _sources(conn, op, ops, legacy_parents):
    op_id = op.get('client_op_id')
    row = conn.execute('SELECT rowid,exposure_history FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
    if row is not None:
        return [('kanban_sync_outbox', row)]
    row = conn.execute('SELECT rowid,exposure_history FROM kanban_sync_parent_envelopes '
                       "WHERE json_extract(wire,'$.client_op_id')=?", (op_id,)).fetchone()
    if row is not None:
        return [('kanban_sync_parent_envelopes', row)]
    sources = []
    # Only the legacy batch builder can declare an intentionally unsaved parent.
    # Its pending children own the support operation's exposure, not its capture
    # identity. A lost persisted/frozen parent must still refuse, never downgrade.
    if op in legacy_parents and not op.get('frozen_replay'):
        for child in ops:
            refs = (str(child.get('uid', '')).split('>') if child.get('table') == 'task_links'
                    else [child.get('fields', {}).get('task_id')])
            if op.get('uid') in refs:
                row = conn.execute('SELECT rowid,exposure_history FROM kanban_sync_outbox WHERE op_id=?',
                                   (child.get('client_op_id'),)).fetchone()
                if row is None:
                    raise ExposureCustodyError('exposure child custody missing')
                sources.append(('kanban_sync_outbox', row))
    if not sources:
        raise ExposureCustodyError('exposure source custody missing')
    return sources


def _observation_capability(capability):
    """Unsupported or non-object capability is unidentified, never a principal set."""
    return capability if isinstance(capability, dict) else {}


def _observe(conn, ops, board, endpoint, ident, capability, *, legacy_parents=()):
    """Append under the writer reservation; COMMIT is a prerequisite to sending."""
    capability = _observation_capability(capability)
    principal = capability.get('gateway_principal')
    incarnation = capability.get('board_incarnation')
    authenticated = all(isinstance(value, str) and value for value in (principal, incarnation))
    with write_txn(conn):
        for op in ops:
            for table, row in _sources(conn, op, ops, legacy_parents):
                _append(conn, table, row, op, board, endpoint, ident, principal, incarnation, authenticated)


def _append(conn, table, row, op, board, endpoint, ident, principal, incarnation, authenticated):
    history = json.loads(row[1]) if row[1] is not None else dict(
        protocol='exposure1', coverage='unknown', certified=False, exposures=[])
    if (history.get('protocol') != 'exposure1' or history.get('certified') is not False or
            history.get('coverage') not in ('unknown', 'candidate') or not isinstance(history.get('exposures'), list)):
        raise ExposureCustodyError('unsupported exposure history; preserve custody')
    exposure = dict(board=board, endpoint=endpoint, url=ident.url,
                    credential_epoch=hashlib.sha256(ident.key.encode()).hexdigest(),
                    gateway_principal=principal if authenticated else None,
                    board_incarnation=incarnation if authenticated else None, wire=op)
    if exposure not in history['exposures']:
        history['exposures'].append(exposure)
    conn.execute(f'UPDATE {table} SET exposure_history=? WHERE rowid=?',
                 (json.dumps(history, sort_keys=True, separators=(',', ':'), allow_nan=False), row[0]))


@contextmanager
def replay_session(hub, board):
    """One immutable credential snapshot owns capability, session and ops I/O.

    Reuse is confined to this sender turn, never across reenrollment or boards.
    The closure retains its own capability copy so callers cannot relabel an
    exposure by changing the returned capability. Each exposure still commits
    before its operation body; a connection failure never skips that commit.
    """
    from hermes_cli.kanban_sync_client import Hub
    root = f'/v2/boards/{quote(board, safe="")}/replay/'
    transport = Hub(hub.ident, timeout=hub.timeout, deadline=hub.deadline)
    try:
        from hermes_cli.kanban_replay_reply import capability_envelope
        capability = capability_envelope(transport._call('GET', root + 'capability'))
        observed_capability = dict(capability)

        def send_ops(conn, ops, replay):
            if conn.in_transaction:
                raise ValueError('exposure requires a standalone durable commit before I/O')
            _observe(conn, ops, board, root + 'ops', transport.ident, observed_capability)
            return transport._call('POST', root + 'ops', dict(replay, ops=ops))

        yield transport, capability, send_ops
    finally:
        transport.close()


def send(hub, conn, board, ops, *, replay=None, legacy_parents=()):
    """Pin the credentials for capability AND mutation, including header creation.

    A fresh transport prevents a caller replacing hub.ident during the probe from
    substituting credentials after the observation. Unsupported legacy capability
    yields an explicitly unidentified observation, never a complete principal set.
    """
    from hermes_cli.kanban_sync_client import Hub, HubError, Resync
    if conn.in_transaction:
        raise ValueError('exposure requires a standalone durable commit before I/O')
    root = f'/v2/boards/{quote(board, safe="")}/'
    endpoint = root + ('replay/ops' if replay is not None else 'ops')
    transport = Hub(hub.ident, timeout=hub.timeout, deadline=hub.deadline)
    try:
        try:
            capability = transport._call('GET', root + 'replay/capability')
        except Resync:
            # A legacy hub answers the optional probe 410; that is "no replay capability", not a cursor rebuild.
            capability = {}
        except HubError as exc:
            if exc.status not in (404, 405, 501, 503):
                raise
            capability = {}
        _observe(conn, ops, board, endpoint, transport.ident, _observation_capability(capability),
                 legacy_parents=legacy_parents if replay is None else ())
        body = dict(replay, ops=ops) if replay is not None else dict(ops=ops)
        return transport._call('POST', endpoint, body)
    finally:
        transport.close()
