"""Current live-row projection for the dormant primary replay sender. [daveey]

Historical slot verdicts never contain row images. Reads happen outside the
writer; projection is joined explicitly to the owning journal settlement.
"""
from hermes_cli import kanban_db_sync as sync


class PrimarySettlement:
    def __init__(self, conn, hub, board):
        self.conn, self.hub, self.board = conn, hub, board
        self.current = None
        self.deleted = False
        self.staged = None
        self.projected = None

    def read(self, row, verdict):
        """Read separately authenticated live state, not a historical result."""
        self.current = None
        self.deleted = False
        from hermes_cli.kanban_replay_repair import retryable
        if retryable(row, verdict):
            return  # Legacy transient refusal never projects an image over unacked edits.
        if verdict['status'] not in ('applied', 'merged', 'rejected', 'deferred'):
            raise ValueError('primary replay disposition not integrated')
        from hermes_cli.kanban_replay_existence import CurrentRowHold
        from hermes_cli.kanban_sync_client import HubError, Resync
        try:
            evidence = self.hub.row_evidence(self.board, row['table_name'], row['row_uid'])
        except (OSError, HubError, Resync) as exc:
            raise CurrentRowHold(f'primary replay live current row unavailable: {exc}', transient=True) from exc
        image = evidence.get('row')
        if (evidence.get('board') != self.board or evidence.get('table') != row['table_name'] or
                evidence.get('uid') != row['row_uid'] or
                type(evidence.get('head')) is not int or evidence['head'] < 0):
            raise CurrentRowHold('primary replay live current row unavailable: unbound evidence')
        if (row['op'] == 'delete' and verdict['status'] in ('applied', 'merged') and
                evidence.get('state') == 'deleted' and 'row' in evidence and image is None):
            # The exact delete already removed the local row. Authenticated current
            # absence permits settlement, not a synthetic row image or deletion of
            # a later local recreation. Unknown/never evidence cannot do this.
            self.deleted = True
            return
        if (evidence.get('state') != 'live' or
                not isinstance(image, dict) or image.get('uid') != row['row_uid'] or
                type(image.get('version')) is not int or image['version'] <= 0 or
                image.get('deleted_at') is not None):
            raise CurrentRowHold('primary replay live current row unavailable: ' + str(evidence.get('state')))
        self.current = image

    def stage(self, row, wire, verdict):
        from hermes_cli.kanban_replay_repair import retryable
        if retryable(row, verdict):
            return
        if self.current is None and not self.deleted:
            raise ValueError('primary replay current row not read')
        row = dict(row, fields=wire.get('fields', {}))
        if verdict['status'] in ('applied', 'merged') and not self.deleted:
            from hermes_cli.kanban_sync_omissions import acknowledge
            acknowledge(self.conn, wire['client_op_id'], self.current, sent=wire.get('fields', {}))
        self.staged = row, wire, verdict

    def finish(self):
        """Called after recover advances the journal, but before outer commit."""
        if self.staged is None:
            return
        row, wire, verdict = self.staged
        self.staged = None
        if self.deleted:
            return  # Current absence has no row image to project over newer local work.
        from hermes_cli import kanban_sync_client as client
        changes = []
        if verdict['status'] == 'rejected':
            reason = f"{verdict.get('reason')}: {verdict.get('detail') or ''}".strip(': ')
            if verdict.get('reason') == 'forbidden_field':
                from hermes_cli.kanban_sync_compat import VIRTUAL_DROPPED, enqueue_retry, record_rejection
                if enqueue_retry(self.conn, row, wire, verdict) != VIRTUAL_DROPPED:
                    record_rejection(self.conn, row, reason)
            else:
                from hermes_cli import kanban_sync_live_claim as claims
                if claims.is_live_claim(row, verdict):
                    # This is separately authenticated CURRENT state. Unknown never
                    # enters this branch and cannot authorize phantom-row deletion.
                    restore = True
                    if row['table_name'] == 'task_runs':
                        local = self.conn.execute('SELECT sync_version FROM task_runs WHERE uid=?',
                                                  (row['row_uid'],)).fetchone()
                        restore = local is None or local[0] is None or self.current['version'] >= local[0]
                    claims.refused(self.conn, row, dict(verdict, row=self.current), reason, restore, changes)
        if not changes:
            changes.append(client._row_change(row['table_name'], self.current))
        cursor = (sync.sync_state(self.conn) or {}).get('cursor', 0)
        self.projected = sync.apply_remote_changes(
            self.conn, changes, new_cursor=cursor, board=self.board,
            observers=False, local_pending_wins=True, allow_nested=True)

    def after_commit(self):
        if self.projected is not None:
            result, self.projected = self.projected, None
            sync._after_commit(self.conn, result, self.board)
            from hermes_cli.kanban_source_refusal_block import drain
            drain(self.conn)
