"""Acceptance: real core against the real Board for grant -> delegate renewal -> revoke.

Run with HERMES_TEST_BOARD_SOURCE pointing at a hermes-hub checkout whose Board advertises ``scope_delegate: 1``
(as ``test_kanban_real_board_failure``). No fake Hub: the ops, the grant read, the capability and the renewal
readback are the Worker's own.
"""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_scope_delegate as delegates
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_scope_renewal_cli as cli
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_scope_renewal import RenewalRefused
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_real_board_failure as bridge_support

board_peer = bridge_support.board_peer
GATEWAY = 'failure-fixture'
TRIAGE = '20261123_101500_a1b2c3'
AGENT = ('agent', TRIAGE, '4242:HERMES_SESSION_ID')
HUMAN = ('human', None, 'clean-ancestry')


class SharedHub:
    """The fixture's one authenticated client, handed to code that opens (and closes) its own."""

    def __init__(self, hub):
        self._hub = hub

    def __getattr__(self, name):
        return getattr(self._hub, name)

    def close(self):
        pass


def refused(code, fn, *a, **kw):
    with pytest.raises(RenewalRefused) as caught:
        fn(*a, **kw)
    assert caught.value.code == code, str(caught.value)


def test_grant_delegate_renewal_and_revoke_through_the_real_board(tmp_path, monkeypatch, board_peer):
    _bridge, hub = board_peer
    capability = hub.decision_retain_capability('default')
    if capability.get('scope_delegate') != 1:
        pytest.skip('this Board checkout predates delegated scope renewal')
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    client.ensure_identity(conn, 'default', GATEWAY)
    sync.set_mode(conn, 'primary')
    previous = sync.set_lease_client(client.HubLeaseClient(hub))
    monkeypatch.setattr(client, 'Hub', lambda identity: SharedHub(hub))
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(name, raising=False)

    def renew(comment):
        return cli.renew(conn, tid, episode=episode, comment_id=comment, reason='David said unblock', writer=AGENT,
                         env={}, ancestry=[{'HERMES_SESSION_ID': TRIAGE}])

    def mandate():
        return kb.add_comment(conn, tid, 'default', 'Renewed scope: finish the open PR.', writer_kind='agent',
                              writer_session=TRIAGE)
    try:
        # A card its own run scope-stopped through the Board's canonical retain op; no chat owns it.
        tid = kb.create_task(conn, title='real Board delegate fixture', assignee='worker', runs_on=[])
        assert client.push(conn, hub, 'default', GATEWAY, primary=True).rejected == 0
        client.pull(conn, hub, 'default', observers=True)
        assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=kb._host_prefix() + 'fixture')
        assert client.push(conn, hub, 'default', GATEWAY, primary=True).rejected == 0
        client.pull(conn, hub, 'default', observers=True)
        run = conn.execute('SELECT uid FROM task_runs WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)',
                           (tid,)).fetchone()[0]
        receipt = sync.uuid7()
        stop, = hub.ops('default', [{
            'client_op_id': sync.uuid7(), 'op': 'retain', 'task': tid, 'run_uid': run, 'event_uid': receipt,
            'expected_episode': None,
            'payload': {'version': 1, 'summary': 'Incomplete scope', 'reason': 'scope_stopped',
                        'next_step': 'Owner decides', 'release_condition': 'Scoped authorization',
                        'evidence': ['fixture']}}])['results']
        assert stop['status'] == 'applied', stop
        client.pull(conn, hub, 'default', observers=True)
        with kbc.write_txn(conn):  # the stopped worker's own exit clears its local claim; no worker runs here
            conn.execute('UPDATE tasks SET claim_lock=NULL, claim_expires=NULL WHERE id=?', (tid,))
        assert kb.get_task(conn, tid).status == 'triage' and decision.execution_restricted(conn, tid)
        episode = renewal.key(decision.pending_episode(conn, tid))

        # Only a human grants; the Board refuses a gateway it has never heard from and a 31-day '*'.
        refused('not_human', delegates.grant, conn, session=TRIAGE, reason='self-service', writer=AGENT)
        refused('unknown_gateway', delegates.grant, conn, session=TRIAGE, gateway='never-called', reason='x',
                writer=HUMAN)
        assert delegates.grants(conn) == []
        refused('not_scope_authority', renew, mandate())

        first = delegates.grant(conn, session=TRIAGE, reason='triage chat may unblock cards', writer=HUMAN, hours=1)
        assert (first['store'], first['gateway'], first['session'], first['live'], first['mine']) == (
            'board', GATEWAY, TRIAGE, True, True)
        assert first['expires_at'] - first['granted_at'] == 3600
        assert delegates.revoke(conn, session=TRIAGE, reason='not yet', writer=HUMAN) == [first['uid']]
        ended, = delegates.grants(conn)
        assert (ended['live'], ended['revoked_by_gateway'], ended['revoke_reason']) == (False, GATEWAY, 'not yet')
        refused('not_scope_authority', renew, mandate())  # a revoked grant is no authority
        assert decision.execution_restricted(conn, tid)

        wide = delegates.grant(conn, session='*', reason='triage chats may unblock cards', writer=HUMAN)
        payload = renew(mandate())
        assert (payload['authority_kind'], payload['authority_ref'], payload['actor_session'], payload['actor_gateway']) \
            == ('delegate', wide['uid'], TRIAGE, GATEWAY)
        event, = conn.execute("SELECT uid, payload FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,))
        assert json.loads(event[1])['authority_ref'] == wide['uid']
        assert hub.row_evidence('default', 'task_events', event[0])['state'] == 'live'
        # Renewal is not promotion: still triage, the stop is still the pending decision, nothing restricted.
        assert kb.get_task(conn, tid).status == 'triage'
        assert decision.pending(conn, tid) and not decision.execution_restricted(conn, tid)

        assert delegates.revoke(conn, session='*', reason='done', writer=HUMAN) == [wide['uid']]
        assert [g['live'] for g in delegates.grants(conn)] == [False, False]
        assert delegates.live_grant(conn, None, gateway=GATEWAY, profile='default', session=TRIAGE) is None
    finally:
        sync.set_lease_client(previous)
        conn.close()
