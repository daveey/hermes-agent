"""Synced boards: the canonical Board decides a scope renewal; this gateway only asks and projects its event."""
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_scope_renewal_cli as cli
from hermes_cli import kanban_spend_decision as decision
from hermes_cli.kanban_origin import gateway_names
from hermes_cli.kanban_scope_renewal import RenewalRefused
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live
CHAT = '20261112_052539_8d8534'
AGENT = ('agent', CHAT, '4242:HERMES_SESSION_ID')


class Hub:
    def __init__(self, capability, reply=None):
        self.capability, self.reply, self.sent = capability, reply, []
        self.ident = SimpleNamespace(gateway='fixture')

    def decision_retain_capability(self, board):
        return self.capability

    def ops(self, board, ops):
        self.sent += ops
        if self.reply:
            return {'results': [{'client_op_id': ops[0]['client_op_id'], **self.reply}]}
        return {'results': [{'client_op_id': ops[0]['client_op_id'], 'status': 'applied'}]}

    def row_evidence(self, board, table, uid):
        wire = self.sent[-1]
        payload = {'version': 1, 'episode': wire['episode'], 'mandate_comment_uid': wire['mandate_comment_uid'],
                   'mandate_body_sha256': hashlib.sha256(wire['mandate_body'].encode()).hexdigest(),
                   'covered_restriction_uids': [wire['episode']], 'actor_gateway': 'fixture', **wire['payload']}
        now = int(time.time())
        return {'board': board, 'table': table, 'uid': uid, 'state': 'live', 'row': {
            'uid': uid, 'task_id': wire['task'], 'run_uid': None, 'kind': 'scope_renewed', 'payload': json.dumps(payload),
            'created_at': now, 'version': 1, 'hlc': f'{now * 1000:013d}-0000-server', 'origin_gateway': 'fixture',
            'updated_at': now * 1000, 'deleted_at': None}}

    def close(self):
        pass


def stopped(live, monkeypatch, hub):
    from hermes_cli import kanban_sync_client as client
    tid, _ = support._running(live)
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status='triage',claim_lock=NULL,claim_expires=NULL WHERE id=?", (tid,))
        kb._append_event(live, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': 'out of scope'})
        live.execute("INSERT INTO task_chat_links (task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES (?,?,NULL,?,'created',0,1)", (tid, gateway_names()[0], CHAT))
    comment = kb.add_comment(live, tid, 'default', 'Renewed scope: one correction round.', writer_kind='agent',
                             writer_session=CHAT)
    monkeypatch.setattr(client, 'Hub', lambda identity: hub)
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'push', lambda *a, **k: None)
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK'):
        monkeypatch.delenv(name, raising=False)
    return tid, renewal.key(decision.pending_episode(live, tid)), comment


def renew(live, tid, episode, comment):
    return cli.renew(live, tid, episode=episode, comment_id=comment, reason='authorized', writer=AGENT, env={},
                     ancestry=[{'HERMES_SESSION_ID': CHAT}])


def test_board_applies_and_the_exact_event_is_projected(live, monkeypatch):
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = stopped(live, monkeypatch, hub)
    assert sync.board_mode(live) != 'off'
    before = live.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0]
    payload = renew(live, tid, episode, comment)
    wire, = hub.sent
    uid = live.execute('SELECT uid FROM task_comments WHERE id=?', (comment,)).fetchone()[0]
    assert wire == {'client_op_id': wire['event_uid'], 'op': 'renew_scope', 'task': tid, 'event_uid': wire['event_uid'],
                    'episode': episode, 'mandate_comment_uid': uid, 'mandate_body': 'Renewed scope: one correction round.',
                    'payload': {'version': 1, 'actor_kind': 'agent', 'actor_session': CHAT, 'actor_profile': 'default',
                                'authority_kind': 'chat',
                                'authority_ref': f'@session:default@{gateway_names()[0]}/{CHAT}', 'reason': 'authorized'}}
    assert payload['covered_restriction_uids'] == [episode]
    row = live.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchall()
    assert [r[0] for r in row] == [wire['event_uid']]
    # The Board's event came in as a remote row: nothing new waits in this gateway's outbox.
    assert live.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0] == before
    assert not decision.execution_restricted(live, tid) and decision.pending(live, tid)
    assert kb.get_task(live, tid).status == 'triage'


@pytest.mark.parametrize('capability,reply,code', [
    ({'decision_retain': 1}, None, 'board_unsupported'),
    ({'decision_retain': 1, 'scope_renewal': 1}, {'status': 'rejected', 'reason': 'not_scope_authority'}, 'not_scope_authority'),
    ({'decision_retain': 1, 'scope_renewal': 1}, {'status': 'deferred'}, 'board_rejected'),
])
def test_unsupported_or_refusing_board_changes_nothing(live, monkeypatch, capability, reply, code):
    hub = Hub(capability, reply)
    tid, episode, comment = stopped(live, monkeypatch, hub)
    with pytest.raises(RenewalRefused) as caught:
        renew(live, tid, episode, comment)
    assert caught.value.code == code
    assert bool(hub.sent) is (reply is not None)
    assert not live.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()
    assert decision.execution_restricted(live, tid)


def test_local_validation_refuses_before_any_board_request(live, monkeypatch):
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, _, comment = stopped(live, monkeypatch, hub)
    with pytest.raises(RenewalRefused):
        renew(live, tid, 'not-the-episode', comment)
    assert not hub.sent


def refused_without_projection(live, tid, episode, comment, code):
    with pytest.raises(RenewalRefused) as caught:
        renew(live, tid, episode, comment)
    assert caught.value.code == code
    assert not live.execute("SELECT 1 FROM task_events WHERE kind='scope_renewed'").fetchone()
    assert decision.execution_restricted(live, tid)


def test_synced_board_with_sync_inactive_never_renews_locally(live, monkeypatch):
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = stopped(live, monkeypatch, hub)
    assert sync.sync_state(live)
    monkeypatch.setattr(sync, 'board_mode', lambda conn: 'off')
    refused_without_projection(live, tid, episode, comment, 'board_unavailable')
    assert not hub.sent


def test_board_event_read_back_for_another_task_projects_nothing(live, monkeypatch):
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = stopped(live, monkeypatch, hub)
    real = hub.row_evidence

    def wrong(board, table, uid):
        proof = real(board, table, uid)
        proof['row']['task_id'] = 't_00000000deadbeef'
        return proof
    hub.row_evidence = wrong
    refused_without_projection(live, tid, episode, comment, 'board_unconfirmed')


@pytest.mark.parametrize('damage', ['state', 'kind', 'row'])
def test_board_row_that_is_not_a_live_renewal_projects_nothing(live, monkeypatch, damage):
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = stopped(live, monkeypatch, hub)
    real = hub.row_evidence

    def wrong(board, table, uid):
        proof = real(board, table, uid)
        if damage == 'state':
            proof['state'] = 'deleted'
        elif damage == 'kind':
            proof['row']['kind'] = 'diagnostic'
        else:
            proof['row'] = None
        return proof
    hub.row_evidence = wrong
    refused_without_projection(live, tid, episode, comment, 'board_unconfirmed')


@pytest.mark.parametrize('results', [[], [{'client_op_id': 'someone-elses-op', 'status': 'applied'}],
                                     [{'status': 'applied'}, {'status': 'applied'}]])
def test_board_reply_that_does_not_answer_this_request_is_refused(live, monkeypatch, results):
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = stopped(live, monkeypatch, hub)

    def ops(board, sent):
        hub.sent += sent
        return {'results': [{'client_op_id': sent[0]['client_op_id'], **r} if 'client_op_id' not in r and len(results) > 1
                            else r for r in results]}
    hub.ops = ops
    hub.row_evidence = lambda *a: pytest.fail('an unanswered request must not be read back')
    refused_without_projection(live, tid, episode, comment, 'board_rejected')


@pytest.mark.parametrize('capability', [None, 'scope_renewal', {'scope_renewal': 2}, {'scope_renewal': 0, 'x': 1}])
def test_board_without_exactly_scope_renewal_v1_is_never_asked(live, monkeypatch, capability):
    hub = Hub(capability)
    tid, episode, comment = stopped(live, monkeypatch, hub)
    refused_without_projection(live, tid, episode, comment, 'board_unsupported')
    assert not hub.sent


@pytest.mark.parametrize('mode,pushes', [('primary', 1), ('shadow', 0)])
def test_primary_pushes_the_mandate_before_asking_and_no_other_mode_pushes(live, monkeypatch, mode, pushes):
    from hermes_cli import kanban_sync_client as client
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = stopped(live, monkeypatch, hub)
    calls = []

    def push(conn, target, board, gateway, **kw):
        assert not hub.sent, 'the mandate comment must be canonical before the Board is asked'
        calls.append((target, gateway, kw))
    monkeypatch.setattr(client, 'push', push)
    monkeypatch.setattr(sync, 'board_mode', lambda conn: mode)
    renew(live, tid, episode, comment)
    assert calls == [(hub, 'fixture', {'primary': True})] * pushes
    assert len(hub.sent) == 1
