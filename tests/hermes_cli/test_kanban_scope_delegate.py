"""Delegated scope renewal: a chat session a human granted renewal rights renews cards it does not own."""
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scope_delegate as delegates
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_scope_renewal_cli as cli
from hermes_cli import kanban_spend_decision as decision
from hermes_cli.kanban_origin import gateway_names
from hermes_cli.kanban_scope_renewal import RenewalRefused
from tests.hermes_cli import test_kanban_scope_renewal as base
from tests.hermes_cli import test_kanban_scope_renewal_synced as synced

board, live = base.board, synced.live
TRIAGE = '20261123_101500_a1b2c3'  # a desk chat: no link to the stopped card, no run of it
AGENT = ('agent', TRIAGE, '4242:HERMES_SESSION_ID')
HUMAN = base.HUMAN


def grant(conn, session=TRIAGE, **kw):
    return delegates.grant(conn, session=session, reason='triage chat may unblock cards', writer=HUMAN, **kw)


def refused(code, fn, *a, **kw):
    with pytest.raises(RenewalRefused) as caught:
        fn(*a, **kw)
    assert caught.value.code == code, str(caught.value)


# ---- grants on a board that never synced -------------------------------------------------------------------

def test_a_human_grants_and_the_grant_is_listed_live(board):
    made = grant(board, hours=2)
    here = gateway_names()[0]
    assert made['store'] == 'local' and made['live'] and made['mine']
    assert (made['gateway'], made['profile'], made['session']) == (here, 'default', TRIAGE)
    assert made['expires_at'] - made['granted_at'] == 7200 and made['reason'] == 'triage chat may unblock cards'
    assert [g['uid'] for g in delegates.grants(board)] == [made['uid']]
    assert delegates.grant(board, session=TRIAGE, reason='default window', writer=HUMAN)['expires_at'] \
        - int(time.time()) in range(168 * 3600 - 5, 168 * 3600 + 1)


@pytest.mark.parametrize('writer', [AGENT, ('agent', None, '4242:HERMES_AGENT'), ('agent', TRIAGE, 'inconclusive:pid 1'),
                                    ('agent', None, 'runner:systemd:x.service')])
def test_an_agent_can_neither_grant_nor_revoke(board, writer):
    grant(board)
    refused('not_human', delegates.grant, board, session='*', reason='self-service', writer=writer)
    refused('not_human', delegates.revoke, board, session=TRIAGE, reason='x', writer=writer)
    assert [(g['session'], g['live']) for g in delegates.grants(board)] == [(TRIAGE, True)]


def test_star_lasts_30_days_and_one_session_366(board):
    assert grant(board, session='*', hours=30 * 24)['expires_at'] - int(time.time()) in range(30 * 86400 - 5, 30 * 86400 + 1)
    assert grant(board, hours=366 * 24)['live']
    with pytest.raises(RenewalRefused, match='at most 30 days'):
        grant(board, session='*', hours=31 * 24)


def test_the_real_caller_classifier_decides_who_is_human(board, monkeypatch):
    """No writer handed in: the modelled process ancestry is read, as ``renew-scope`` reads it."""
    env = base.ancestry(monkeypatch, {'HERMES_SESSION_ID': TRIAGE}, {'HERMES_AGENT': '1'}, {})
    refused('not_human', delegates.grant, board, session=TRIAGE, reason='x', env=env)
    assert delegates.grants(board) == []
    env = base.ancestry(monkeypatch, {}, {'TERM': 'xterm'}, {})
    assert delegates.grant(board, session=TRIAGE, reason='x', env=env)['live']


def test_bad_input_is_refused_and_writes_nothing(board):
    refused('invalid_session', grant, board, session='two words')
    refused('invalid_session', grant, board, session='')
    refused('invalid_reason', delegates.grant, board, session=TRIAGE, reason=' ', writer=HUMAN)
    for hours in (0, -1, 0.001, 400 * 24, float('nan'), float('inf')):
        refused('invalid_hours', grant, board, hours=hours)
    refused('invalid_hours', grant, board, session='*', hours=30 * 24 + 1)  # '*' is wide: 30 days at most
    refused('no_live_grant', delegates.revoke, board, session=TRIAGE, reason='x', writer=HUMAN)
    assert delegates.grants(board) == []


def test_regrant_supersedes_and_list_keeps_ended_grants(board):
    first, wide = grant(board), grant(board, session='*')
    second = grant(board)
    board.execute("UPDATE scope_delegates SET expires_at=? WHERE uid=?", (int(time.time()) - 1, wide['uid']))
    board.commit()
    state = {g['uid']: (g['live'], g['revoked_at'] is not None, g['revoke_reason']) for g in delegates.grants(board)}
    assert state == {first['uid']: (False, True, f"superseded by {second['uid']}"),
                     wide['uid']: (False, False, None), second['uid']: (True, False, None)}
    assert delegates.revoke(board, session=TRIAGE, reason='done', writer=HUMAN) == [second['uid']]
    assert not any(g['live'] for g in delegates.grants(board))
    ended = {g['uid']: g for g in delegates.grants(board)}[second['uid']]
    assert (ended['revoked_by_gateway'], ended['revoked_by_profile'], ended['revoke_reason']) == (
        gateway_names()[0], 'default', 'done')


# ---- renewal by a delegate ---------------------------------------------------------------------------------

def renew(conn, tid, episode, comment, *, writer=AGENT, env=None):
    return base.renew(conn, tid, episode, comment, writer=writer, env=env, reason='David said unblock')


def restricted(conn, tid, code, *a, **kw):
    refused(code, renew, conn, tid, *a, **kw)
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()
    assert decision.execution_restricted(conn, tid)


def test_a_delegate_with_an_exact_session_grant_renews_a_card_it_does_not_own(board, monkeypatch):
    tid, _, episode = base.stopped(board, monkeypatch)
    comment = base.mandate(board, tid, session=TRIAGE)
    restricted(board, tid, 'not_scope_authority', episode, comment)  # no grant: still not the owning chat
    # The refusal prints the whole grant command for THIS chat, so it works typed on another machine.
    with pytest.raises(RenewalRefused) as hint:
        cli.resolve_actor(board, tid, writer=AGENT, environs=[{'HERMES_SESSION_ID': TRIAGE}], gateway='mbp',
                          profile='work')
    assert f'`hermes kanban delegate grant --session {TRIAGE} --gateway mbp --profile work --reason <why>`' in str(hint.value)
    made = grant(board)
    payload = renew(board, tid, episode, comment)
    assert {k: payload[k] for k in ('actor_kind', 'actor_session', 'authority_kind', 'authority_ref')} == {
        'actor_kind': 'agent', 'actor_session': TRIAGE, 'authority_kind': 'delegate', 'authority_ref': made['uid']}
    # As for every authority: renewal is not promotion.
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid) and not decision.execution_restricted(board, tid)
    assert kb.promote_task(board, tid, actor='fixture', reason='renewed scope')[0] is True


def test_a_star_grant_covers_every_chat_session_of_that_gateway_and_profile(board, monkeypatch):
    tid, _, episode = base.stopped(board, monkeypatch)
    wide = grant(board, session='*')
    other = '20261123_111111_ffeedd'
    comment = base.mandate(board, tid, session=other)
    payload = renew(board, tid, episode, comment, writer=('agent', other, '7:HERMES_SESSION_ID'))
    assert (payload['authority_kind'], payload['authority_ref'], payload['actor_session']) == ('delegate', wide['uid'], other)


def test_an_exact_grant_is_named_before_a_star_grant(board):
    wide, exact = grant(board, session='*'), grant(board)
    here = gateway_names()[0]
    assert delegates.live_grant(board, None, gateway=here, profile='default', session=TRIAGE) == exact['uid']
    assert delegates.live_grant(board, None, gateway=here, profile='default', session='another') == wide['uid']
    assert delegates.live_grant(board, None, gateway=here, profile='work', session=TRIAGE) is None
    assert delegates.live_grant(board, None, gateway='another-gateway', profile='default', session=TRIAGE) is None


def test_expired_and_revoked_grants_refuse(board, monkeypatch):
    tid, _, episode = base.stopped(board, monkeypatch)
    comment = base.mandate(board, tid, session=TRIAGE)
    made = grant(board)
    board.execute("UPDATE scope_delegates SET expires_at=? WHERE uid=?", (int(time.time()) - 1, made['uid']))
    board.commit()
    restricted(board, tid, 'not_scope_authority', episode, comment)
    grant(board)
    delegates.revoke(board, session=TRIAGE, reason='done', writer=HUMAN)
    restricted(board, tid, 'not_scope_authority', episode, comment)
    grant(board, gateway='another-gateway')
    grant(board, profile='work')
    grant(board, session='20261123_222222_abcdef')
    restricted(board, tid, 'not_scope_authority', episode, comment)


def test_a_delegate_that_is_a_run_of_the_card_is_refused(board, monkeypatch):
    tid, session, episode = base.stopped(board, monkeypatch)
    grant(board, session='*')
    grant(board, session=session)
    comment = base.mandate(board, tid, session=session)
    restricted(board, tid, 'own_run', episode, comment, writer=('agent', session, '9:HERMES_SESSION_ID'))
    restricted(board, tid, 'own_run', episode, comment, env={'HERMES_KANBAN_WORKER_TASK': tid})
    restricted(board, tid, 'own_run', episode, comment, env={'HERMES_KANBAN_TASK': tid})


def test_a_caller_with_a_task_marker_never_becomes_a_delegate(board, monkeypatch):
    """A worker of another card stays creator-task-or-refused, even holding the widest grant."""
    tid, _, episode = base.stopped(board, monkeypatch)
    grant(board, session='*')
    grant(board, session='local-worker')
    other = kb.create_task(board, title='another card', assignee='default')
    board.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
    kb._append_event(board, other, 'owner_admitted', {'session_id': 'local-worker'})
    board.commit()
    comment = base.mandate(board, tid, session='local-worker')
    for marker in ('HERMES_KANBAN_WORKER_TASK', 'HERMES_KANBAN_TASK'):
        restricted(board, tid, 'not_scope_authority', episode, comment,
                   writer=('agent', 'local-worker', f'7:{marker}'), env={marker: other})
    # The marker counts on any ancestor, even when the caller's own environment dropped it.
    env = base.ancestry(monkeypatch, {'HERMES_SESSION_ID': 'local-worker'},
                        {'HERMES_SESSION_ID': 'local-worker', 'HERMES_KANBAN_WORKER_TASK': other}, {})
    refused('not_scope_authority', cli.renew, board, tid, episode=episode, comment_id=comment, reason='x', env=env)
    assert decision.execution_restricted(board, tid)


@pytest.mark.parametrize('writer', [('agent', None, '4242:HERMES_AGENT'), ('agent', TRIAGE, 'inconclusive:pid 1'),
                                    ('agent', TRIAGE, 'runner:systemd:x.service'), ('agent', TRIAGE, 'author_handle')])
def test_an_ambiguous_identity_is_refused_before_any_grant_is_read(board, monkeypatch, writer):
    tid, _, episode = base.stopped(board, monkeypatch)
    grant(board, session='*')
    monkeypatch.setattr(delegates, 'live_grant', lambda *a, **k: pytest.fail('identity comes first'))
    restricted(board, tid, 'identity_unavailable', episode, base.mandate(board, tid, session=TRIAGE), writer=writer)


def test_a_mandate_written_by_a_different_session_or_a_human_is_refused(board, monkeypatch):
    tid, _, episode = base.stopped(board, monkeypatch)
    grant(board)
    restricted(board, tid, 'mandate_authority', episode, base.mandate(board, tid, session='someone-else'))
    restricted(board, tid, 'mandate_authority', episode, base.mandate(board, tid, kind='human'))
    task_note = base.mandate(board, tid, session=TRIAGE)  # the same session, but written from inside a Kanban task
    board.execute("UPDATE task_events SET payload=json_set(payload,'$.writer_task','t_00000000deadbeef') WHERE id="
                  "(SELECT max(id) FROM task_events WHERE task_id=? AND kind='commented')", (tid,))
    board.commit()
    restricted(board, tid, 'mandate_authority', episode, task_note)
    assert renew(board, tid, episode, base.mandate(board, tid, session=TRIAGE))['authority_kind'] == 'delegate'


def test_a_mandate_written_before_the_stop_is_stale_for_a_delegate_too(board, monkeypatch):
    tid, _, episode = base.stopped(board, monkeypatch)
    grant(board)
    comment = base.mandate(board, tid, session=TRIAGE)
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': episode})
    board.commit()
    restricted(board, tid, 'mandate_stale', episode, comment)


def test_a_safety_restriction_has_no_delegated_renewal(board, monkeypatch):
    tid, _, episode = base.stopped(board, monkeypatch)
    grant(board, session='*')
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal', 'episode': episode})
    board.commit()
    restricted(board, tid, 'safety_restricted', episode, base.mandate(board, tid, session=TRIAGE))


def test_the_owning_chat_stays_the_owning_chat_and_no_grant_is_read(board, monkeypatch):
    tid, _, episode = base.stopped(board, monkeypatch)
    base.link(board, tid)
    grant(board, session='*')
    monkeypatch.setattr(delegates, 'live_grant', lambda *a, **k: pytest.fail('the owning chat needs no grant'))
    assert base.renew(board, tid, episode, base.mandate(board, tid))['authority_kind'] == 'chat'


def test_the_command_refuses_an_agent_and_prints_grants(board, monkeypatch, capsys):
    from hermes_cli import kanban_db_connect as kbc
    from contextlib import contextmanager

    @contextmanager
    def same():
        yield board
    monkeypatch.setattr(kbc, 'connect_closing', same)
    base.ancestry(monkeypatch, {'HERMES_SESSION_ID': TRIAGE}, {})
    args = SimpleNamespace(delegate_action='grant', session=TRIAGE, gateway=None, profile=None, hours=168.0,
                           reason='x', json=True)
    assert delegates.command(args) == 1 and '"error": "not_human"' in capsys.readouterr().out
    base.ancestry(monkeypatch, {}, {})
    monkeypatch.delenv('HERMES_SESSION_ID', raising=False)
    assert delegates.command(args) == 0 and '"store": "local"' in capsys.readouterr().out
    assert delegates.command(SimpleNamespace(delegate_action='list', json=False)) == 0
    assert 'live     ' in capsys.readouterr().out
    args.delegate_action = 'revoke'
    assert delegates.command(args) == 0 and '"revoked": [' in capsys.readouterr().out
    assert delegates.command(SimpleNamespace(delegate_action='list', json=False)) == 0
    assert 'revoked  ' in capsys.readouterr().out


def test_the_parser_wires_grant_revoke_and_list():
    import argparse
    from hermes_cli import kanban, kanban_parser
    root = argparse.ArgumentParser()
    kanban_parser.build_parser(root.add_subparsers(dest='command'))
    args = root.parse_args(['kanban', 'delegate', 'grant', '--session', '*', '--reason', 'why'])
    assert (args.kanban_action, args.delegate_action, args.session, args.hours, args.gateway, args.profile) == (
        'delegate', 'grant', '*', 168.0, None, None)
    args = root.parse_args(['kanban', 'delegate', 'revoke', '--session', TRIAGE, '--gateway', 'hub', '--profile', 'p',
                            '--reason', 'why'])
    assert (args.delegate_action, args.gateway, args.profile) == ('revoke', 'hub', 'p')
    assert root.parse_args(['kanban', 'delegate', 'list', '--json']).json is True
    with pytest.raises(SystemExit):
        root.parse_args(['kanban', 'delegate', 'grant', '--session', '*'])  # --reason is required
    import inspect
    assert '"delegate": lambda a: importlib.import_module("hermes_cli.kanban_scope_delegate")' in inspect.getsource(kanban)


# ---- synced boards: the Board holds the grants -------------------------------------------------------------

class Hub(synced.Hub):
    """The Board's delegate contract: two ops, one private table, one read."""

    def __init__(self, capability, reply=None):
        super().__init__(capability, reply)
        self.grants, self.now = [], int(time.time())

    def scope_delegates(self, board):
        assert self.capability.get('scope_delegate') == 1, 'never asked without the capability'
        return {'version': 1, 'now': self.now, 'grants': [
            {**g, 'live': g['revoked_at'] is None and g['expires_at'] > self.now, 'mine': g['gateway'] == self.ident.gateway}
            for g in reversed(self.grants)]}

    def ops(self, board, ops):
        reply = super().ops(board, ops)
        op = ops[0]
        if reply['results'][0]['status'] != 'applied' or op['op'] == 'renew_scope':
            return reply
        key = lambda g: (g['gateway'], g['profile'], g['session']) == (op['gateway'], op['profile'], op['session'])
        ended = [g for g in self.grants if key(g) and g['revoked_at'] is None]
        for g in ended:
            g.update(revoked_at=self.now, revoked_by_gateway='fixture', revoke_reason=op['payload']['reason'])
        if op['op'] == 'delegate_scope_grant':
            self.grants.append({'uid': op['grant_uid'], 'gateway': op['gateway'], 'profile': op['profile'],
                                'session': op['session'], 'granted_at': self.now, 'expires_at': self.now + op['ttl_s'],
                                'granted_by_gateway': 'fixture', 'granted_by_profile': op['payload']['actor_profile'],
                                'reason': op['payload']['reason'], 'revoked_at': None, 'revoked_by_gateway': None,
                                'revoke_reason': None})
        else:
            reply['results'][0]['detail'] = ','.join(g['uid'] for g in ended)
        return reply


BOTH = {'decision_retain': 1, 'scope_renewal': 1, 'scope_delegate': 1}


def unowned(live, monkeypatch, hub):
    """A scope-stopped card on a synced board whose only mandate is the triage chat's."""
    tid, episode, _ = synced.stopped(live, monkeypatch, hub)
    with kb.write_txn(live):
        live.execute("DELETE FROM task_chat_links WHERE task_id=?", (tid,))
    comment = kb.add_comment(live, tid, 'default', 'Renewed scope: finish the open PR.', writer_kind='agent',
                             writer_session=TRIAGE)
    return tid, episode, comment


def renew_synced(live, tid, episode, comment):
    return cli.renew(live, tid, episode=episode, comment_id=comment, reason='David said unblock', writer=AGENT, env={},
                     ancestry=[{'HERMES_SESSION_ID': TRIAGE}])


def test_synced_grant_and_revoke_are_board_ops_with_a_human_claim(live, monkeypatch):
    hub = Hub(BOTH)
    synced.stopped(live, monkeypatch, hub)
    here = gateway_names()[0] or hub.ident.gateway  # the Board identity names a host with no mail registry
    made = grant(live, hours=1)
    wire, = hub.sent
    assert wire == {'client_op_id': made['uid'], 'op': 'delegate_scope_grant', 'grant_uid': made['uid'], 'gateway': here,
                    'profile': 'default', 'session': TRIAGE, 'ttl_s': 3600,
                    'payload': {'version': 1, 'actor_kind': 'human', 'actor_profile': 'default',
                                'reason': 'triage chat may unblock cards'}}
    assert made['store'] == 'board' and made['live'] and made['mine']
    assert not live.execute("SELECT 1 FROM sqlite_master WHERE name='scope_delegates'").fetchone(), 'no local copy'
    assert [g['uid'] for g in delegates.grants(live)] == [made['uid']]
    assert delegates.revoke(live, session=TRIAGE, reason='done', writer=HUMAN) == [made['uid']]
    assert hub.sent[-1] == {'client_op_id': hub.sent[-1]['client_op_id'], 'op': 'delegate_scope_revoke', 'gateway': here,
                            'profile': 'default', 'session': TRIAGE,
                            'payload': {'version': 1, 'actor_kind': 'human', 'actor_profile': 'default', 'reason': 'done'}}
    assert [g['live'] for g in delegates.grants(live)] == [False]
    # An agent never reaches the Board, and the Board's refusal is the caller's.
    refused('not_human', delegates.grant, live, session='*', reason='x', writer=AGENT)
    assert len(hub.sent) == 2
    hub.reply = {'status': 'rejected', 'reason': 'not_human'}
    refused('not_human', grant, live)
    hub.reply = {'status': 'rejected', 'reason': 'no_live_grant'}
    refused('no_live_grant', delegates.revoke, live, session=TRIAGE, reason='x', writer=HUMAN)


def test_synced_delegate_renewal_names_the_boards_grant(live, monkeypatch):
    hub = Hub(BOTH)
    tid, episode, comment = unowned(live, monkeypatch, hub)
    refused('not_scope_authority', renew_synced, live, tid, episode, comment)
    assert not hub.sent
    made = grant(live, session='*')
    payload = renew_synced(live, tid, episode, comment)
    wire = hub.sent[-1]
    assert wire['op'] == 'renew_scope' and wire['payload'] == {
        'version': 1, 'actor_kind': 'agent', 'actor_session': TRIAGE, 'actor_profile': 'default',
        'authority_kind': 'delegate', 'authority_ref': made['uid'], 'reason': 'David said unblock'}
    assert payload['authority_kind'] == 'delegate'
    assert [r[0] for r in live.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,))] \
        == [wire['event_uid']]
    assert not decision.execution_restricted(live, tid) and kb.get_task(live, tid).status == 'triage'


def test_synced_expired_revoked_or_foreign_grants_do_not_make_a_delegate(live, monkeypatch):
    hub = Hub(BOTH)
    tid, episode, comment = unowned(live, monkeypatch, hub)
    grant(live)
    hub.grants[-1]['expires_at'] = hub.now - 1
    grant(live, session='*', gateway='another-gateway')
    grant(live, session='*', profile='work')
    grant(live, session='*')
    delegates.revoke(live, session='*', reason='done', writer=HUMAN)
    sent = len(hub.sent)
    refused('not_scope_authority', renew_synced, live, tid, episode, comment)
    assert len(hub.sent) == sent and decision.execution_restricted(live, tid)


def test_an_old_board_without_the_flag_gives_board_unsupported(live, monkeypatch):
    hub = Hub({'decision_retain': 1, 'scope_renewal': 1})
    tid, episode, comment = unowned(live, monkeypatch, hub)
    refused('board_unsupported', grant, live)
    refused('board_unsupported', delegates.revoke, live, session=TRIAGE, reason='x', writer=HUMAN)
    refused('board_unsupported', delegates.grants, live)
    # A renewal carrying delegate authority is never sent to it ...
    actor = renewal.Actor('agent', TRIAGE, 'delegate', 'grant-from-a-newer-board', gateway_names()[0], 'default')
    refused('board_unsupported', cli._renew, live, live, tid, episode, comment, 'x', None, actor)
    # ... and such a Board holds no grant, so the chat's own refusal is what it always was.
    refused('not_scope_authority', renew_synced, live, tid, episode, comment)
    assert not hub.sent and decision.execution_restricted(live, tid)


def test_a_board_that_cannot_be_read_grants_and_renews_nothing(live, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    hub = Hub(BOTH)
    tid, episode, comment = unowned(live, monkeypatch, hub)

    def down(board):
        raise client.HubError(503, 'rollover')
    hub.scope_delegates = down
    refused('board_unavailable', grant, live)
    # Pinned: the capability read succeeded and the grant read failed. A chat with no grant is told the Board is
    # unavailable, not that it lacks authority: with the grants unread, "no grant" would hide a real outage from a
    # chat that does hold one.
    refused('board_unavailable', renew_synced, live, tid, episode, comment)
    hub.scope_delegates = lambda board: {'version': 2, 'grants': []}
    refused('board_unavailable', delegates.grants, live)
    assert not hub.sent
    monkeypatch.setattr(sync, 'board_mode', lambda conn: 'off')  # synced, but sync is not active here
    refused('board_unavailable', grant, live)
    refused('board_unavailable', delegates.grants, live)
    refused('not_scope_authority', renew_synced, live, tid, episode, comment)  # as before delegates existed
    assert not live.execute("SELECT 1 FROM sqlite_master WHERE name='scope_delegates'").fetchone()
