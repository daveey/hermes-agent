"""W=1 production push entry for already bound strict journals. [daveey]

No enrollment or activation. This WIP handles ordinary upsert/delete intents and
parent-first transport, but still refuses unintegrated modes/control/dispositions.
Provenance, drainage and all custody consumers must pass before it can ship.
"""
import json
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_controls as controls
from hermes_cli import kanban_replay_recovery as recovery
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_replay_reconcile import reconcile


# Physical operation stays in the outbox; only the transport spelling is normalized.
_WIRE_OPS = {'upsert': 'upsert', 'delete': 'delete', 'compat': 'upsert',
             'compat_import': 'upsert', 'import': 'upsert', 'restore': 'upsert',
             'retain': 'retain', 'actionability_record': 'actionability_record', 'release': 'release'}


def _wire_op(physical):
    try:
        return _WIRE_OPS[physical]
    except KeyError:
        raise ValueError('tracer operation kind not integrated') from None


class ReplayFault(ValueError):
    """A strict stream refused progress; preserve custody and isolate its board."""


@dataclass
class PushResult:
    sent: int = 0
    applied: int = 0
    merged: int = 0
    rejected: int = 0
    deferred: int = 0
    cancelled: int = 0


def push(conn, hub, board, gateway, *, primary, max_batches=25, before_head=False, seen=None):
    """Serialize the complete session/send, not only individual SQL writes."""
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_sync_guard import sender_lock
    path = _main_db_file(conn)
    if path is None:
        raise ValueError('sender requires an on-disk board')
    with sender_lock(path):
        return _push(conn, hub, board, gateway, primary=primary, max_batches=max_batches,
                     before_head=before_head, seen=seen)


def _push(conn, hub, board, gateway, *, primary, max_batches, before_head, seen):
    """Select only the journal-bound transport; never auto-enroll or fall back."""
    from hermes_cli import kanban_sync_client as client
    client.refuse_if_schema_ahead(conn)
    from hermes_cli.kanban_replay_admission import guard
    guard(conn)
    if (sync.sync_state(conn) or {}).get('mode') == 'off':
        raise ValueError('sync is detached; sender disabled')
    if journal.bound_custody(conn):
        from hermes_cli import kanban_replay_existence as existence
        existence.guard(conn)
        try:
            with recovery.turn(conn, board):
                return _push_authenticated(conn, hub, board, gateway, max_batches=max_batches,
                                    primary=primary, before_head=before_head, seen={} if seen is None else seen)
        except existence.CurrentRowHold as exc:
            from hermes_cli.kanban_replay_episodes import interrupt
            interrupt(conn)
            existence.pause(conn, board, exc)
            raise
        except ValueError as exc:
            from hermes_cli.kanban_replay_episodes import interrupt
            interrupt(conn)
            raise ReplayFault(str(exc)) from exc
        except (OSError, client.HubError, client.Resync) as exc:
            from hermes_cli.kanban_replay_episodes import interrupt
            interrupt(conn)
            if isinstance(exc, client.Resync):
                # Only feed reads own cursor resynchronization. A strict endpoint
                # 410 is transport failure; let ordinary rounds keep pulling.
                raise client.HubError(410, str(exc)) from exc
            raise
    return client._push_legacy(conn, hub, board, gateway, primary=primary,
                               max_batches=max_batches, before_head=before_head, seen=seen)


def _push_authenticated(conn, hub, board, gateway, *, max_batches, primary, before_head, seen):
    from hermes_cli import kanban_sync_client as client
    if isinstance(hub, client._HTTP_HUB):
        journal.snapshot(conn)  # Corrupt local custody refuses before even a read-only probe.
        from hermes_cli.kanban_sync_exposure import replay_session
        with replay_session(hub, board) as (transport, capability, send_ops):
            recovery.reachable()
            return _push_strict(conn, transport, board, gateway, max_batches=max_batches,
                                primary=primary, before_head=before_head, seen=seen,
                                capability=capability, send_ops=send_ops)
    return _push_strict(conn, hub, board, gateway, max_batches=max_batches,
                        primary=primary, before_head=before_head, seen=seen)


def _push_strict(conn, hub, board, gateway, *, max_batches, primary, before_head, seen,
                 capability=None, send_ops=None):
    from hermes_cli import kanban_sync_client as client
    root = f'/v2/boards/{quote(board, safe="")}/replay/'
    out = client.PushResult()
    local = journal.snapshot(conn)
    if isinstance(hub, client._HTTP_HUB) and send_ops is None:
        raise ValueError('strict HTTP transport requires pinned session')
    from hermes_cli.kanban_replay_reply import capability_envelope, slot_envelope
    cap = capability_envelope(recovery.call(hub, 'GET', root + 'capability') if capability is None else capability)
    if (cap.get('processed_prefix') is not True or
            cap.get('board_incarnation') != local['board_incarnation'] or
            cap.get('gateway_principal') != local['gateway_principal']):
        raise ValueError('replay capability unavailable')
    remote = slot_envelope(recovery.call(hub, 'GET', root + 'inspect'))
    reconcile(local, remote)  # inspect before authenticated full-state session CAS
    identity = dict(board_incarnation=local['board_incarnation'], journal_id=local['journal_id'])
    expected = dict(remote, session=str(int(remote['session']) + 1))
    remote = slot_envelope(recovery.call(hub, 'POST', root + 'session', dict(identity, expected=remote)))
    if remote != expected:
        raise ValueError('session readback mismatch')
    reconcile(local, remote)
    remote = _ack_settled(conn, hub, root, identity, remote)
    recovery.completed(conn)

    def source():
        pending = journal.snapshot(conn)['pending']
        row = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],)).fetchone()
        wire = json.loads(journal.pending_wire(conn))
        del wire['replay']
        table, uid = controls.routing(wire)
        if row is None:
            if wire['op'] in controls.CONTROLS:
                raise ValueError('issued command source custody missing')
            saved = conn.execute('SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?', (wire['uid'],)).fetchone()
            if saved is None or json.loads(saved[0]) != wire:
                raise ValueError('issued source custody missing')
            return dict(row_uid=wire['uid'], table_name='tasks', op='upsert', _parent=True)
        if row['op'] in controls.FROZEN:
            controls.validate(conn, dict(row), wire)
        elif row['wire_json'] is None or json.loads(row['wire_json']) != wire:
            raise ValueError('issued source custody changed')
        if (row['row_uid'] != uid or row['table_name'] != table or
                pending.get('source_op', wire['op']) != row['op'] or
                _wire_op(row['op']) != wire['op'] or row['state'] != 'pending' or
                bool(wire.get('import')) != (row['table_name'] == 'tasks' and row['op'] in ('import', 'compat_import'))):
            raise ValueError('issued source custody routing changed')
        return dict(row)

    membership = None
    current_row = None
    lagged = set()
    refused = []
    from hermes_cli.kanban_replay_primary import PrimarySettlement
    projection = PrimarySettlement(conn, hub, board) if primary else None

    @contextmanager
    def settlement():
        # This process-local receipt protects dispatcher copy eviction. Never
        # publish it from record(): its owning transaction may still roll back.
        from hermes_cli import kanban_replica_lag as lag
        from hermes_cli import kanban_replay_existence as existence
        with write_txn(conn):
            existence.guard(conn)
            yield
            if projection is not None:
                projection.finish()
            from hermes_cli.kanban_replay_repair import remap_refusal
            for row, verdict in refused:
                remap_refusal(conn, row, verdict)
            refused.clear()
            existence.clear_settled(conn)
        if lagged:
            lag.acked(client.shadow_path(board), lagged)
            lagged.clear()
        if projection is not None:
            projection.after_commit()

    def _parent_projectable(uid):
        return conn.execute('SELECT 1 FROM tasks WHERE id=?', (uid,)).fetchone() is not None and not conn.execute(
            "SELECT 1 FROM kanban_sync_tombstones WHERE table_name='tasks' AND row_uid=?", (uid,)).fetchone()

    def read_current_row():
        """Read current state separately; never enrich the historical slot verdict."""
        decision = reconcile(journal.snapshot(conn), remote)
        if decision['action'] != 'record':
            return None
        verdict = decision['verdict']
        if source()['op'] == 'release' and verdict['status'] == 'deferred':
            raise ValueError('unsupported final command disposition; preserve custody')
        if source()['op'] in controls.FROZEN or verdict['status'] == 'cancelled':
            return None
        if (verdict['status'] == 'deferred' and cap.get('deferred_observation') == 'origin_pending1'
                and (projection is not None or source().get('_parent'))):
            # The authenticated final verdict establishes server status custody
            # independently of the optional current-image read. A later read or
            # projection failure must not erase that known semantic obligation.
            from hermes_cli import kanban_replay_deferred as deferred
            with write_txn(conn):
                source()
                reconcile(journal.snapshot(conn), remote)
                wire = json.loads(journal.pending_wire(conn))
                captured = deferred.captures(conn, local['board_incarnation'], wire['uid'])
                if not any(item['origin'] == wire.get('status_origin') and
                           item['status'] == wire.get('fields', {}).get('status') and
                           item['hlc'] == wire['hlc'] for item in captured):
                    deferred.handoff(conn, wire, verdict, local['board_incarnation'], membership=membership)
        if projection is not None:
            projection.read(source(), verdict)
            return client._row_change('tasks', projection.current) if source().get('_parent') and projection.current else None
        if verdict['status'] not in ('applied', 'merged', 'deferred'):
            return None
        row = source()
        if not row.get('_parent'):
            return None
        if verdict['status'] != 'deferred' and verdict.get('version') and not _parent_projectable(row['row_uid']):
            return None  # Locally deleted: the authoritative version needs no image.
        # A versioned verdict still needs the current image: the issued feed echo
        # was skipped to keep custody, so this read is the only projection of it.
        from hermes_cli.kanban_replay_existence import CurrentRowHold
        try:
            from hermes_cli.kanban_replay_reply import row_evidence_envelope
            evidence = row_evidence_envelope(hub.row_evidence(board, 'tasks', row['row_uid']),
                                             conn=conn, table='tasks')
        except (OSError, client.HubError, client.Resync, ValueError) as exc:
            raise CurrentRowHold(f'synthetic parent current row unavailable: {exc}', transient=True) from exc
        state = evidence.get('row')
        if (evidence.get('board') != board or evidence.get('table') != 'tasks' or
                evidence.get('uid') != row['row_uid'] or evidence.get('state') != 'live' or
                type(evidence.get('head')) is not int or not 0 <= evidence['head'] < 1 << 63 or
                not isinstance(state, dict) or state.get('uid') != row['row_uid'] or
                type(state.get('version')) is not int or state['version'] <= 0 or
                state.get('deleted_at') is not None):
            raise CurrentRowHold('synthetic parent current row unavailable: ' + str(evidence.get('state')))
        return client._row_change('tasks', state)

    def refresh_current_row():
        from hermes_cli.kanban_replay_existence import CurrentRowHold
        state = journal.snapshot(conn)
        pending = state['pending']
        try:
            return read_current_row()
        except CurrentRowHold as exc:
            exc.expected = (state['journal_id'], state['generation'], pending['sequence'], pending['op_id'])
            raise

    def refresh_capacity():
        from hermes_cli import kanban_replay_deferred as deferred
        current = journal.snapshot(conn)
        decision = reconcile(current, remote)
        if decision['action'] != 'record' or decision['verdict']['status'] != 'deferred':
            return None
        wire = json.loads(journal.pending_wire(conn))
        if wire['op'] in controls.CONTROLS:
            return None
        active = [item for item in deferred.pending(conn)
                  if item['board_incarnation'] == current['board_incarnation'] and item['task'] == wire['uid']]
        if len(active) < 16 or cap.get('deferred_observation') != 'origin_pending1':
            return None
        with write_txn(conn):
            active = deferred.captures(conn, current['board_incarnation'], wire['uid'])
        source()  # Frozen source must still exist before any reconciliation read.
        reply = hub._call('GET', root + 'deferred?task=' + quote(wire['uid'], safe=''))
        return deferred.validate_membership(reply, current, wire['uid']), active

    def project_parent(row):
        if current_row is None or current_row.uid != row['row_uid']:
            raise ValueError('synthetic parent authoritative version unavailable')
        # Current existence is independent of whether the requested status applied.
        # Pending fields and newer feed images still win under ordinary projection.
        cursor = (sync.sync_state(conn) or {}).get('cursor', 0)
        if conn.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name='tasks' AND row_uid=?",
                        (row['row_uid'],)).fetchone():
            from hermes_cli.kanban_replay_existence import CurrentRowHold
            raise CurrentRowHold('synthetic parent current projection held by local deletion')
        sync.apply_remote_changes(conn, [current_row], new_cursor=cursor, board=board,
                                  observers=False, local_pending_wins=True, allow_nested=True)

    def record(verdict):
        pending = journal.snapshot(conn)['pending']
        if verdict['client_op_id'] != pending['op_id']:
            raise ValueError('source verdict identity mismatch')
        row = source()
        status = verdict['status']
        if row['op'] in controls.FROZEN:
            result = controls.settle(conn, row, verdict)
            setattr(out, result, getattr(out, result) + 1)
            return
        if projection is not None and status != 'cancelled':
            projection.stage(row, json.loads(journal.pending_wire(conn)), verdict)
        if status in ('rejected', 'cancelled'):
            if row.get('_parent'):
                # Move exact synthetic custody into the ordinary visible repair queue.
                # This is not a new intent, a retry, or an authoritative row success.
                from time import time
                from hermes_cli.kanban_sync_envelope import retire_parent
                wire = json.loads(journal.pending_wire(conn))
                del wire['replay']
                conn.execute('INSERT INTO kanban_sync_outbox '
                             '(op_id,table_name,row_uid,op,fields,hlc,created_at,status_origin,wire_json) '
                             'VALUES (?,?,?,?,?,?,?,?,?)',
                             (pending['op_id'], 'tasks', row['row_uid'], 'upsert', json.dumps(wire['fields']),
                              wire['hlc'], int(time() * 1000), wire.get('status_origin'), json.dumps(wire)))
                retire_parent(conn, row['row_uid'], remote=remote)
                row = dict(conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?',
                                        (pending['op_id'],)).fetchone())
            reason = f"{verdict.get('reason') or status}: {verdict.get('detail') or ''}".strip(': ')
            if status == 'cancelled':
                # Reuse non-success custody so dependency/import fences also hold
                # cancelled intents. The label and retained verdict distinguish them.
                reason = f'cancelled: {reason}'
            conn.execute("UPDATE kanban_sync_outbox SET state='rejected', last_error=?, attempts=attempts+1 "
                         "WHERE op_id=?", (reason[:500], pending['op_id']))
            setattr(out, status, getattr(out, status) + 1)
            if status == 'rejected' and not row.get('_parent'):
                refused.append((row, verdict))
            return  # Keep the semantic obligation visible; no sync success or identity rotation.
        if status == 'deferred':
            if cap.get('deferred_observation') != 'origin_pending1':
                raise ValueError('tracer final disposition requires custody integration')
            from hermes_cli.kanban_replay_deferred import handoff
            handoff(conn, json.loads(journal.pending_wire(conn)), verdict, local['board_incarnation'],
                    membership=membership)
            if row.get('_parent'):
                project_parent(row)
                from hermes_cli.kanban_sync_envelope import retire_parent
                retire_parent(conn, row['row_uid'], remote=remote)
            else:
                conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],))
            out.deferred += 1
            return  # Transport handoff is not authoritative row success.
        if status not in ('applied', 'merged'):
            raise ValueError('tracer final disposition requires custody integration')
        if row.get('_parent') and (not verdict.get('version') or current_row is not None):
            project_parent(row)
        if verdict.get('version') and row['table_name'] == 'tasks' and row['op'] != 'release':
            conn.execute('UPDATE tasks SET sync_version=MAX(COALESCE(sync_version,0),?) WHERE id=?',
                         (verdict['version'], row['row_uid']))
        elif verdict.get('version') and row['table_name'] == 'task_runs':
            conn.execute('UPDATE task_runs SET sync_version=MAX(COALESCE(sync_version,0),?) WHERE uid=?',
                         (verdict['version'], row['row_uid']))
        if row.get('_parent'):
            from hermes_cli.kanban_sync_envelope import retire_parent
            retire_parent(conn, row['row_uid'], remote=remote)
        else:
            conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],))
        from hermes_cli import kanban_replica_lag as lag
        wire = json.loads(journal.pending_wire(conn))
        lagged.update(lag.acked_task_ids([dict(row, fields=wire.get('fields', {}))]))
        setattr(out, status, getattr(out, status) + 1)

    for _ in range(max_batches):
        from hermes_cli.kanban_sync_parent_order import parent_first, select_pending
        from time import time
        rows = []
        if journal.pending_wire(conn) is None:
            # one replay capability is pinned per turn; the sender never certifies owner reassessment
            rows, _, _ = select_pending(conn, hub, board, seen, now=int(time()), limit=1, before_head=before_head,
                                        origin_cap={},
                                        hold_rejected=True)
            from hermes_cli.kanban_actionability_transport import before_send
            before_send(conn, rows, hub, board)

        membership = refresh_capacity()
        current_row = refresh_current_row()
        with settlement():
            decision = journal.recover(conn, remote, record)
            if decision['action'] == 'record':
                break  # recovery settles exactly one; no newly selected intent this turn
            encoded = journal.pending_wire(conn)
            if encoded is None:
                if not rows:
                    break
                current = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (rows[0]['op_id'],)).fetchone()
                if current is None or dict(current) != rows[0]:
                    raise ValueError('selected source changed before issuance')
                rows = parent_first(conn, rows, int(time()), 1, hold_rejected=True)
                if not rows:
                    break
                row = rows[0]
                _wire_op(row['op'])
                from hermes_cli import kanban_sync_envelope as envelopes
                from hermes_cli import kanban_sync_wire as wire_ops
                from hermes_cli.kanban_terminal_guard import strip_stale_push
                parents = client._unacked_parents(conn, rows, gateway)
                if parents:
                    op, tid = parents[0]
                    intent = envelopes.parent(conn, tid, lambda: op, supported=True)
                elif row['op'] in controls.FROZEN:
                    from hermes_cli.kanban_actionability_transport import outbound
                    intent = outbound(conn, row)
                else:
                    intent = envelopes.outbound(conn, row, lambda: strip_stale_push(conn, wire_ops.translate(
                        client._full_row_if_unseen(conn, row), client._next_hlc(conn, gateway))), supported=True)
                    if conn.execute('SELECT wire_json FROM kanban_sync_outbox WHERE op_id=?',
                                    (row['op_id'],)).fetchone()[0] is None:
                        raise ValueError('uncaptured source status cannot enter replay journal')
                encoded = journal.issue(conn, intent, source_op=None if parents else row['op'])
            row = source()  # Validate retained source custody on RETRY as well as final recovery.
        wire = json.loads(encoded)
        from hermes_cli.kanban_actionability_transport import before_send
        before_send(conn, [row], hub, board)
        if before_head:
            # Issuance is not permission to overwrite an unread run end or terminal
            # task. Judge even saved unissued envelopes by their frozen wire.
            from hermes_cli.kanban_sync_backlog import sendable_before_head
            candidate = dict(row, fields=wire.get('fields', {}))
            allowed, _ = sendable_before_head(conn, hub, board, [candidate], seen)
            if not allowed:
                break
        wire['replay']['session'] = remote['session']
        recovery.guard(conn, mutation=True)
        try:
            if send_ops is not None:
                reply = send_ops(conn, [wire], identity)
            else:
                reply = recovery.call(hub, 'POST', root + 'ops', dict(identity, ops=[wire]))
        except (OSError, client.HubError, client.Resync):
            # Inspect immediately, but leave recording/ACK to the next serialized
            # turn. A lost mutation reply never authorizes a blind resend.
            try:
                reconcile(journal.snapshot(conn), slot_envelope(recovery.call(hub, 'GET', root + 'inspect')))
            except (OSError, client.HubError, client.Resync):
                pass  # Offline inspection preserves the original transport error/custody.
            raise
        out.sent += 1
        from hermes_cli.kanban_replay_reply import consumed_slot
        remote = (consumed_slot(reply, wire, remote, journal.snapshot(conn)) or
                  slot_envelope(recovery.call(hub, 'GET', root + 'inspect')))
        membership = refresh_capacity()
        current_row = refresh_current_row()
        with settlement():
            decision = journal.recover(conn, remote, record)
        if decision['action'] != 'record':
            break
        recovery.completed(conn)
        # The next issued envelope freezes the durable predecessor ACK. Keep
        # standalone ACK for idle/end-of-turn and recovery, not between sends.
    settled = journal.snapshot(conn)
    remote = _ack_settled(conn, hub, root, identity, remote)
    from hermes_cli.kanban_replay_observation import after_push
    after_push(conn, hub, root, settled, cap)
    return out


def _ack_settled(conn, hub, root, identity, remote):
    settled = journal.snapshot(conn)
    # An exact retained successor can already be ACKed by another copy. Its
    # authenticated prefix must never be moved back to our settled predecessor.
    if int(remote['a']) < int(settled['settled']):
        expected = dict(remote, a=settled['settled'])
        from hermes_cli.kanban_replay_reply import slot_envelope
        remote = slot_envelope(recovery.call(hub, 'POST', root + 'ack', dict(identity, session=remote['session'],
            generation=settled['generation'], ack=dict(sequence=settled['settled'],
                op_id=settled['last_op_id'], payload_sha256=settled['last_payload_sha256']))))
        from hermes_cli.kanban_replay_reply import same_slot
        if not same_slot(expected, remote):
            raise ValueError('ack readback mismatch')
        reconcile(settled, remote)
    return remote
