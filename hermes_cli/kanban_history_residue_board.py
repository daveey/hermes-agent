"""Configured-board entry points for immutable historical audit custody. [daveey]"""
from __future__ import annotations

from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import time

from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli import kanban_history_residue_cut as cut
from hermes_cli import kanban_history_residue_http as transport
from hermes_cli.kanban_history_residue_source import Refused, _require, decode_source, strict_object


def ledger_path(board):
    from hermes_cli.kanban_sync_client import shadow_path
    return shadow_path(board).with_suffix('.history-residue.db')


def _paths(live, shadow, board):
    from hermes_cli.kanban_db import kanban_db_path
    from hermes_cli.kanban_sync_client import shadow_path
    paths = [Path(c.execute('PRAGMA database_list').fetchone()[2]).resolve() for c in (live, shadow)]
    expected = [kanban_db_path(board=board).resolve(), shadow_path(board).resolve()]
    _require(paths == expected and paths[0] != paths[1], 'configured_paths_required')
    return paths


def _ledger(board, *, create=False):
    path = ledger_path(board)
    _require(not path.is_symlink(), 'custody_symlink')
    if create and not path.exists():
        # Parent is the existing managed shadow directory. Never initialize a live board here.
        try:
            with path.open('xb'):
                pass
            path.chmod(0o600)
        except FileExistsError:
            pass
    return sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=0)


def audit_diff(live, shadow, hub, board, *, pulled, started):
    """Fresh nonbusy CLI diff only; report is built while final local reservations hold."""
    from hermes_cli.kanban_sync_guard import board_sync_lock
    paths = _paths(live, shadow, board)
    _require(not pulled.busy and not pulled.more, 'diagnostic_refresh_incomplete')
    with board_sync_lock(paths[0]), closing(_ledger(board)) as ledger:
        identity = transport.scope(hub, board)
        rows = ledger.execute(f'SELECT op_id FROM {custody.INTENTS} WHERE endpoint=? AND board=? AND gateway=? '
                              'ORDER BY op_id LIMIT ?', (*identity, cut.MAX_OPS + 1)).fetchall()
        _require(bool(rows) and len(rows) <= cut.MAX_OPS, 'cut_ops_bound')
        return cut.verify(live, shadow, ledger, hub, board, [r[0] for r in rows], started=started)


def observe_board(board, op_id, reason):
    """Explicit historical observation, never retirement/replay or execution release.

    Existing intent is GET-only. Stage while exact local inputs are reserved, then
    release all SQLite reservations before HTTP. Uncertain delivery stays the same
    immutable intent, even if a process exited before sending the first POST.
    """
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_db import kanban_db_path
    from hermes_cli.kanban_sync_guard import board_sync_lock

    started = time.monotonic()
    _require(isinstance(reason, str) and bool(reason.strip()), 'reason_required')
    ident = client.load_identity()
    hub = client.Hub(ident)
    with ExitStack() as stack:
        stack.callback(hub.close)
        path = kanban_db_path(board=board)
        pulled = client._pull_replica(board, hub, ident.gateway, path, diagnostic=True)
        _require(not pulled.busy and not pulled.more, 'diagnostic_refresh_incomplete')
        stack.enter_context(board_sync_lock(path))
        live = kbc.connect(path)
        shadow = client.open_shadow(board, ident.gateway)
        stack.callback(live.close)
        stack.callback(shadow.close)
        _paths(live, shadow, board)
        ledger = _ledger(board, create=True)
        stack.callback(ledger.close)
        identity = transport.scope(hub, board)
        old = custody.lookup(ledger, identity, op_id)
        if old is None:
            rows = cut.local._rows(live, 'SELECT * FROM kanban_sync_outbox WHERE op_id=?', (op_id,))
            _require(len(rows) == 1, 'source_missing')
            source = json.dumps(rows[0], sort_keys=True, ensure_ascii=True)
            decode_source(source)
        else:
            request, _ = custody._request(old['request_json'])
            _require(request['reason'] == reason, 'intent_conflict')
            source = request['source_json']
        with ExitStack() as reservations:
            for conn in (live, shadow):
                reservations.enter_context(cut._transaction(conn, reserve=True))
            snapshots = cut._capture(live, shadow, source, board, ident.gateway)
            _require(snapshots[0]['portable'] == snapshots[1]['portable'], 'portable_parent_changed')
            request_json = old['request_json'] if old else json.dumps(dict(version=1,
                task=strict_object(decode_source(source)['fields'])['task_id'],
                expected_version=snapshots[0]['portable']['version'], source_json=source, reason=reason))
            cut._fresh(started)
            first = custody.stage_once(ledger, identity, request_json)
        if first:
            try:
                transport._request(hub, 'POST', transport._path(board), request_json.encode('utf-8'))
            except (OSError, client.HubError, Refused, ValueError):
                pass  # Only exact GET can reconcile an uncertain outcome; never POST again.
        return transport.reconcile(ledger, hub, board, op_id)
