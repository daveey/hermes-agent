"""Review probe (t_01a21afd5459bcc7): a never-enrolled LEGACY board's direct push while another push is in flight.

Public API only, so the same file runs on the merge base and on the PR head.
"""
import threading

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support


class LegacyHub:
    """In-process legacy Board: no replay routes, no status-origin capability."""
    def __init__(self, entered=None, release=None):
        self.entered, self.release, self.sent = entered, release, []
        self.ident = client.Identity('http://127.0.0.1:9', 'fixture-key', 'macbook')

    def ops(self, board, ops, **_kwargs):
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(30)
        self.sent.append(ops)
        return {'results': [dict(client_op_id=o['client_op_id'], status='applied', version=1) for o in ops]}


def test_direct_legacy_push_while_round_push_in_flight(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    client.ensure_identity(conn, 'default', 'macbook')
    kb.create_task(conn, title='round row', assignee='worker')
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_replay_sender1'").fetchone()  # never enrolled
    entered, release = threading.Event(), threading.Event()
    slow = LegacyHub(entered, release)
    errors = []

    def round_push():
        other = kbc.connect(home / 'kanban.db')
        try:
            client.push(other, slow, 'default', 'macbook', primary=True, max_batches=1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            other.close()

    thread = threading.Thread(target=round_push)
    thread.start()
    try:
        assert entered.wait(30), 'round push never reached the Board'
        # A worker's direct caller (retain / completion artifacts / scope renewal) pushes now.
        direct = LegacyHub()
        outcome = None
        try:
            client.push(conn, direct, 'default', 'macbook', primary=True, max_batches=1)
            outcome = 'pushed'
        except RuntimeError as exc:
            outcome = f'{type(exc).__name__}: {exc}'
        print('DIRECT_OUTCOME', outcome)
        with open(str(tmp_path / 'outcome.txt'), 'w') as fh:
            fh.write(outcome)
    finally:
        release.set()
        thread.join(30)
        conn.close()
    assert not errors
    assert outcome == 'pushed', outcome
