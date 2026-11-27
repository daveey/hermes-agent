"""Bounded in-process Kanban client for FD alerts; no subprocess or local board lock."""
from __future__ import annotations

import time
from urllib.parse import quote, urlencode

from hermes_cli import kanban_sync_client as sync


class Client:
    """One total 20s budget across request readback/delivery, privately owned connections.

    ``_bounded_call`` closes its connection on success/error/timeout and interrupts stalled
    reads. Persisted incident envelopes make uncertain POSTs safe to retry on the next tick.
    """
    def __init__(self):
        self.identity = sync.load_identity()
        self.deadline = time.monotonic() + 20

    def _call(self, method, path, body=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('fd incident delivery budget exhausted')
        return sync._bounded_call(self.identity, method, path, body, remaining)

    def close(self):
        # Each bounded call already owns and closes its connection; no pooled handle survives.
        pass

    def read(self, tid, board):
        path = f'/v2/boards/{quote(board, safe="")}/evidence?' + urlencode({'table': 'tasks', 'uid': tid})
        evidence = self._call('GET', path)
        if (evidence.get('table') != 'tasks' or evidence.get('uid') != tid
                or type(evidence.get('head')) is not int):
            raise ValueError('invalid authoritative fd incident read')
        state = evidence.get('state')
        if state in {'absent', 'tombstone'}:
            return {'state': state, 'row': None}
        row = evidence.get('row')
        if state != 'live' or not isinstance(row, dict) or not isinstance(row.get('status'), str):
            raise ValueError('unavailable authoritative fd incident read')
        return {'state': 'live', 'row': row}

    def create(self, envelope):
        try:
            response = self._call('GET', '/v2/sync/requests/' + quote(envelope['id'], safe=''))
        except sync.HubError as exc:
            # Only the service's typed missing answer permits resending the frozen request.
            import json
            if exc.status != 404 or json.loads(exc.detail).get('error') != 'no such request':
                raise
        else:
            request = response.get('request')
            if not isinstance(request, dict):
                raise ValueError('invalid fd create receipt')
            if request.get('state') in {'rejected', 'failed', 'expired'}:
                raise RuntimeError(f'fd card request {request["state"]}: {request.get("result")}')
            from hermes_cli.kanban_placement import _task_id_in
            tid = _task_id_in(request.get('result'))
            if tid:
                return tid
            return None  # Accepted but pending: reconcile the SAME request next tick.
        self._call('POST', '/v2/sync/requests', envelope)
        return None

    def comment(self, envelope):
        self._call('POST', '/v2/sync/requests', envelope)
