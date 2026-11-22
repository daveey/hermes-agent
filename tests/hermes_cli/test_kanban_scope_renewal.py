"""Scope renewal: one authority-bound release of one scope stop. Safety stays closed; spend is untouched."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_scope_renewal_cli as cli
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli.kanban_origin import gateway_names
from hermes_cli.kanban_scope_renewal import RenewalRefused
from gateway.session_context import scoped_current_session_id
from tests.hermes_cli import test_kanban_containment_actor as support

board = support.board
ARGS = dict(summary='Incomplete; no execution authority', reason='scope_stopped',
            next_step='Owning supervisor records scope decision', release_condition='Supported explicit authority',
            evidence=['synthetic stopped-scope fixture'])
CHAT = '20261112_052539_8d8534'  # the motivating card's origin chat: an agent writer with no run
AGENT = ('agent', CHAT, '4242:HERMES_SESSION_ID')
HUMAN = ('human', None, 'clean-ancestry')


def stopped(board, monkeypatch):
    """A card its own run scope-stopped, with every worker marker cleared afterwards."""
    from hermes_cli import kanban_retain as retain
    tid, _, session = support.bind(board, monkeypatch)
    with scoped_current_session_id(session):
        result = retain.retain(board, board='default', **ARGS)
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(name, raising=False)
    return tid, session, result['episode_uid']


def link(board, tid, *, session=CHAT, kind='created', released=0, profile='default', gateway=None):
    board.execute("CREATE TABLE IF NOT EXISTS task_chat_links (id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT NOT NULL, "
                  "chat_gateway TEXT NOT NULL, chat_profile TEXT, chat_session TEXT NOT NULL, kind TEXT NOT NULL, "
                  "released INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
    board.execute("INSERT INTO task_chat_links (task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                  "VALUES (?,?,?,?,?,?,1)", (tid, gateway or gateway_names()[0], profile, session, kind, released))
    board.commit()


def mandate(board, tid, *, session=CHAT, kind='agent', body='Renewed scope: one correction round on the open PR.'):
    return kb.add_comment(board, tid, 'default', body, writer_kind=kind, writer_session=session if kind == 'agent' else None)


def renew(board, tid, episode, comment, *, writer=AGENT, env=None, reason='origin chat authorized the round'):
    """``env``: what the caller's shell was started with, next to the session the writer names."""
    started = {**({'HERMES_SESSION_ID': writer[1]} if writer[1] else {}), **(env or {})}
    return cli.renew(board, tid, episode=episode, comment_id=comment, reason=reason, writer=writer, env=started,
                     ancestry=[started])


def ancestry(monkeypatch, *environs):
    """Model the real process walk: this process started with ``environs[0]``, its parent with the next, ..."""
    import os
    from hermes_cli import kanban_comment_writer as writer
    tree = {os.getpid() if i == 0 else 100 + i: (0 if i == len(environs) - 1 else 101 + i, env)
            for i, env in enumerate(environs)}
    monkeypatch.setattr(writer, '_read_proc', lambda pid: tree[pid])
    return environs[0]


def spoofed(board, tid, episode, comment, code, env):
    """The real command path: no writer or ancestry handed in; identity is read from the (modelled) processes."""
    with pytest.raises(RenewalRefused) as caught:
        cli.renew(board, tid, episode=episode, comment_id=comment, reason='x', env=env)
    assert caught.value.code == code
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()
    assert decision.execution_restricted(board, tid)


def refused(board, tid, episode, comment, code, **kw):
    with pytest.raises(RenewalRefused) as caught:
        renew(board, tid, episode, comment, **kw)
    assert caught.value.code == code
    assert not board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()
    assert decision.execution_restricted(board, tid)


def test_a_owning_chat_renews_then_separate_promote_releases(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    payload = renew(board, tid, episode, comment)
    assert {k: payload[k] for k in ('episode', 'actor_kind', 'actor_session', 'reason', 'authority_kind')} == {
        'episode': episode, 'actor_kind': 'agent', 'actor_session': CHAT,
        'reason': 'origin chat authorized the round', 'authority_kind': 'chat'}
    assert payload['mandate_comment_uid'] == renewal.comment_ref(
        dict(board.execute('SELECT * FROM task_comments WHERE id=?', (comment,)).fetchone()))
    # Renewal is not promotion: still triage, still a pending decision, no execution restriction.
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid) and not decision.execution_restricted(board, tid)
    promoted, refusal = kb.promote_task(board, tid, actor='fixture', reason='renewed scope')
    assert promoted is True, refusal
    assert kb.get_task(board, tid).status != 'triage' and not decision.pending(board, tid)
    context = kb.build_worker_context(board, tid)
    assert '## Scope renewal mandate' in context and 'one correction round on the open PR' in context


def test_a_real_origin_chat_shape_null_profile_link_counts_as_default(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid, profile=None)
    link(board, tid, kind='shepherd', released=1)
    comment = mandate(board, tid)
    refused(board, tid, episode, comment, 'not_scope_authority', writer=('agent', 'another-session', '1:HERMES_SESSION_ID'))
    written = json.loads(board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented' "
                                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
    assert written['writer_kind'] == 'agent' and written['writer_session'] == CHAT and 'writer_task' not in written
    assert renew(board, tid, episode, comment)['authority_ref'] == f'@session:default@{gateway_names()[0]}/{CHAT}'


def test_a_human_and_creator_task_renew_on_their_own_mandates(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    agent_note, human_note = mandate(board, tid), mandate(board, tid, kind='human')
    refused(board, tid, episode, agent_note, 'mandate_authority', writer=HUMAN)
    assert renew(board, tid, episode, human_note, writer=HUMAN)['authority_kind'] == 'human'


def test_a_creator_task_renews_only_while_it_runs(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    creator = kb.create_task(board, title='creator', assignee='default')
    board.execute("UPDATE task_events SET payload=json_set(COALESCE(payload,'{}'),'$.creator_task_id',?) "
                  "WHERE task_id=? AND kind='created'", (creator, tid))
    kb._append_event(board, creator, 'owner_admitted', {'session_id': 'local-creator'})
    board.commit()
    comment = mandate(board, tid, session='local-creator')
    env, writer = {'HERMES_KANBAN_WORKER_TASK': creator}, ('agent', 'local-creator', '7:HERMES_KANBAN_WORKER_TASK')
    refused(board, tid, episode, comment, 'not_scope_authority', writer=writer, env=env)  # creator is not running
    board.execute("UPDATE tasks SET status='running' WHERE id=?", (creator,))
    board.commit()
    refused(board, tid, episode, comment, 'not_scope_authority', writer=('agent', 'local-other', '7:X'), env=env)
    assert renew(board, tid, episode, comment, writer=writer, env=env)['authority_ref'] == creator


def test_b_safety_restriction_is_never_renewable(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal', 'episode': episode})
    board.commit()
    refused(board, tid, episode, mandate(board, tid), 'safety_restricted')


def test_b_safety_after_renewal_restricts_and_is_not_covered(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    renew(board, tid, episode, mandate(board, tid))
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal', 'episode': episode})
    board.commit()
    assert decision.execution_restricted(board, tid)
    assert kb.promote_task(board, tid, actor='fixture', reason='x')[0] is False
    assert 'safety' in decision.unblock_refusal(board, tid).lower()


def test_c_stopped_cards_own_run_is_refused(board, monkeypatch):
    tid, session, episode = stopped(board, monkeypatch)
    link(board, tid, session=session)  # even an owning link does not let the card's own run renew
    comment = mandate(board, tid, session=session)
    refused(board, tid, episode, comment, 'own_run', writer=('agent', session, '9:HERMES_SESSION_ID'))
    refused(board, tid, episode, comment, 'own_run', env={'HERMES_KANBAN_WORKER_TASK': tid})
    refused(board, tid, episode, comment, 'own_run', env={'HERMES_KANBAN_TASK': tid})


def test_c_own_run_naming_the_owning_chats_session_with_its_markers_dropped_is_refused(board, monkeypatch):
    """``HERMES_SESSION_ID=<owning chat> env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_WORKER_TASK hermes kanban ...``"""
    tid, session, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)  # written with the same spoofed session
    shell = {'HERMES_KANBAN_WORKER_TASK': tid, 'HERMES_SESSION_ID': session}
    env = ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT}, shell, {})
    spoofed(board, tid, episode, comment, 'own_run', env)
    env = ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT}, {'HERMES_KANBAN_TASK': tid}, {})  # session unbound
    spoofed(board, tid, episode, comment, 'own_run', env)
    env = ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT}, {'HERMES_SESSION_ID': session}, {})
    spoofed(board, tid, episode, comment, 'own_run', env)


def test_d_agent_naming_the_owning_chats_session_is_refused_and_the_chat_itself_renews(board, monkeypatch):
    from hermes_cli import kanban_comment_writer as writer
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    other = {'HERMES_SESSION_ID': 'local-' + 'a' * 64, 'HERMES_KANBAN_WORKER_TASK': 't_00000000deadbeef'}
    env = ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT}, other, {})  # another card's worker
    spoofed(board, tid, episode, comment, 'identity_unavailable', env)
    env = ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT}, {'HERMES_SESSION_ID': '20261101_000000_abcdef'}, {})
    spoofed(board, tid, episode, comment, 'identity_unavailable', env)  # another chat
    ancestry(monkeypatch, {'HERMES_AGENT': '1'}, {'HERMES_SESSION_ID': '20261101_000000_abcdef'}, {})
    spoofed(board, tid, episode, comment, 'identity_unavailable', {'HERMES_SESSION_ID': CHAT})  # set after start

    def unreadable(pid):
        raise writer.Inconclusive('pid 7: environment unreadable')
    monkeypatch.setattr(writer, '_read_proc', unreadable)
    spoofed(board, tid, episode, comment, 'identity_unavailable', {'HERMES_SESSION_ID': CHAT})
    env = ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT, 'HERMES_AGENT': '1'}, {'HERMES_SESSION_ID': CHAT}, {})
    assert cli.renew(board, tid, episode=episode, comment_id=comment, reason='x', env=env)['authority_kind'] == 'chat'


@pytest.mark.parametrize('writer', [AGENT, ('agent', None, '4242:HERMES_AGENT'), ('agent', CHAT, 'inconclusive:pid 1'),
                                    ('agent', CHAT, 'runner:systemd:x.service'), ('agent', CHAT, '5:runner:cron'),
                                    ('agent', CHAT, 'author_handle')])
def test_d_unlinked_or_ambiguous_agent_is_refused(board, monkeypatch, writer):
    tid, _, episode = stopped(board, monkeypatch)
    if writer is not AGENT:
        link(board, tid)
    code = 'not_scope_authority' if writer is AGENT else 'identity_unavailable'
    refused(board, tid, episode, mandate(board, tid), code, writer=writer)


def test_d2_shepherd_touch_and_released_links_are_not_owners(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    comment = mandate(board, tid)
    link(board, tid, kind='shepherd')
    link(board, tid, kind='touch')
    link(board, tid, kind='created', released=1)
    link(board, tid, kind='created', gateway='another-gateway')
    link(board, tid, kind='created', profile='other-profile')
    refused(board, tid, episode, comment, 'not_scope_authority')
    refused(board, tid, episode, comment, 'not_scope_authority')


@pytest.mark.parametrize('reason,owner', [('shepherd', False), ('touch', False), (None, False), ('create', True)])
def test_d2_unsynced_board_only_a_creation_stamped_subscription_owns(board, monkeypatch, reason, owner):
    tid, _, episode = stopped(board, monkeypatch)
    comment = mandate(board, tid)
    from hermes_cli.kanban_db_notify import add_notify_sub
    add_notify_sub(board, task_id=tid, platform='tui', chat_id=CHAT,
                      delivery_metadata={'reason': reason} if reason else None)
    if owner:
        assert renew(board, tid, episode, comment)['authority_kind'] == 'chat'
    else:
        refused(board, tid, episode, comment, 'not_scope_authority')


def test_e_comment_and_generic_promote_alone_leave_the_card_restricted(board, monkeypatch):
    tid, _, _ = stopped(board, monkeypatch)
    link(board, tid)
    mandate(board, tid)
    kb._append_event(board, tid, 'promoted_manual', {})
    kb._append_event(board, tid, 'review_requested', {})
    board.commit()
    assert decision.execution_restricted(board, tid) and decision.pending(board, tid)
    promoted, refusal = kb.promote_task(board, tid, actor='fixture', reason='generic promotion')
    assert promoted is False and 'renew-scope' in refusal


def test_f_wrong_stale_or_unmandated_requests_are_refused(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    refused(board, tid, 'not-the-episode', comment, 'episode_mismatch')
    refused(board, tid, episode, 999999, 'mandate_missing')
    refused(board, tid, episode, mandate(board, tid, session='someone-else'), 'mandate_authority')
    refused(board, tid, episode, mandate(board, tid, body='x' * 8001), 'mandate_too_long')
    refused(board, tid, episode, comment, 'invalid_reason', reason=' ')
    other = kb.create_task(board, title='other', assignee='default')
    refused(board, tid, episode, mandate(board, other), 'mandate_missing')
    # A mandate older than the newest covered restriction is stale.
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': episode})
    board.commit()
    refused(board, tid, episode, comment, 'mandate_stale')
    renew(board, tid, episode, mandate(board, tid))
    with pytest.raises(RenewalRefused) as replay:  # the episode is renewed; nothing is left to cover
        renew(board, tid, episode, mandate(board, tid))
    assert replay.value.code == 'not_scope_stopped'


def test_f_edited_mandate_stops_covering(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    renew(board, tid, episode, comment)
    board.execute("UPDATE task_comments SET body='Do everything' WHERE id=?", (comment,))
    board.commit()
    assert decision.execution_restricted(board, tid)
    assert kb.promote_task(board, tid, actor='fixture', reason='x')[0] is False


@pytest.mark.parametrize('kind', ['spend_budget_triaged', 'decision_hold_restricted'])
def test_g_new_scope_stop_after_renewal_restricts_again(board, monkeypatch, kind):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    renew(board, tid, episode, mandate(board, tid))
    kb._append_event(board, tid, kind, {'cause': 'scope_stopped', 'episode': episode})
    board.commit()
    assert decision.execution_restricted(board, tid) and decision.pending(board, tid)
    assert kb.promote_task(board, tid, actor='fixture', reason='x')[0] is False


def test_g_promotion_older_than_the_scope_annotation_does_not_release_a_renewed_stop(board, monkeypatch):
    tid, _, _ = support.bind(board, monkeypatch)
    board.execute("UPDATE tasks SET status='triage',claim_lock=NULL WHERE id=?", (tid,))
    kb._append_event(board, tid, 'spend_budget_triaged', {'reason': 'budget'})
    episode = renewal.key(decision.pending_episode(board, tid))
    kb._append_event(board, tid, 'promoted_manual', {})
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': episode})
    board.commit()
    link(board, tid)
    renew(board, tid, episode, mandate(board, tid), env={})
    assert decision.pending(board, tid), 'the old promotion must not decide the renewed stop'
    kb._append_event(board, tid, 'promoted_manual', {})
    assert not decision.pending(board, tid)


def test_g_promotion_recorded_before_the_renewal_never_decides_it_whatever_time_it_claims(board, monkeypatch):
    """A refused promote attempt leaves its event behind; its timestamp is its writer's clock, not arrival."""
    tid, _, episode = stopped(board, monkeypatch)
    kb._append_event(board, tid, 'promoted_manual', {})
    board.execute("UPDATE task_events SET created_at=created_at+3600 WHERE task_id=? AND kind='promoted_manual'", (tid,))
    board.commit()
    link(board, tid)
    renew(board, tid, episode, mandate(board, tid))
    assert decision.pending(board, tid), 'a promotion recorded before the renewal must not release it'
    kb._append_event(board, tid, 'promoted_manual', {})  # its clock is an hour behind the stale row's claim
    assert not decision.pending(board, tid), 'one recorded after the renewal is the decision'


def test_h_notice_and_unblock_text_name_the_route_only_for_scope_stops(board, monkeypatch):
    tid, _, _ = stopped(board, monkeypatch)
    text = decision.unblock_refusal(board, tid)
    assert f'hermes kanban renew-scope {tid} --episode' in text and f'hermes kanban promote {tid}' in text
    notice = decision.notice({'cause': 'scope_stopped', 'task_id': tid})
    assert f'hermes kanban renew-scope {tid} --episode' in notice and 'owning chat' in notice
    from types import SimpleNamespace
    from tui_gateway.session_notifications import _kb_spend_budget
    assert f'renew-scope {tid} --episode' in _kb_spend_budget(SimpleNamespace(id=tid), {'cause': 'scope_stopped'}, '')
    safety = decision.notice({'cause': 'safety_refusal', 'task_id': tid})
    assert 'renew-scope' not in safety
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal'})
    assert 'renew-scope' not in decision.unblock_refusal(board, tid)


def test_i_renewal_leaves_the_spend_window_runs_and_tokens_unchanged(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    since, spent = budget._window_start(board, tid, 2999999999), budget.spend(board, tid)
    floor = decision.restricted_floor(board, tid)
    renew(board, tid, episode, comment)
    assert budget._window_start(board, tid, 2999999999) == since
    assert budget.spend(board, tid) == spent
    assert decision.restricted_floor(board, tid) == floor and decision.restricted_since(board, tid) is None


def argv(*args):
    """The installed entry point: argparse, the dispatcher table and the command's own database connection."""
    import argparse
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(['kanban', 'renew-scope', *args]))


def test_j_the_command_line_renews_and_refuses_through_its_own_connection(board, monkeypatch, capsys):
    tid, session, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    request = (tid, '--episode', episode, '--mandate-comment', str(comment), '--reason', 'origin chat authorized', '--json')
    ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT}, {'HERMES_KANBAN_TASK': tid, 'HERMES_SESSION_ID': session}, {})
    monkeypatch.setenv('HERMES_SESSION_ID', CHAT)
    assert argv(*request) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])['error'] == 'own_run'
    assert decision.execution_restricted(board, tid)
    ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT, 'HERMES_AGENT': '1'}, {'HERMES_SESSION_ID': CHAT}, {})
    assert argv(*request) == 0
    reply = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert reply['ok'] and reply['episode'] == episode and reply['authority_kind'] == 'chat'
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()[0] == 1
    assert decision.restricted_since(board, tid) is None
    assert argv(*request[:-1]) == 1  # the stop is already renewed: text output, nothing written twice
    assert 'renew-scope refused' in capsys.readouterr().out
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()[0] == 1


# Guards no earlier test pinned (exact-head review round 3): each case fails if its one clause is dropped.

def test_i_human_mandate_then_renewal_does_not_open_a_fresh_spend_window(board, monkeypatch):
    """A human comment after the stop would move the window if it followed ``restricted_since`` instead of the floor."""
    tid, _, episode = stopped(board, monkeypatch)
    since = budget._window_start(board, tid, 2999999999)
    note = mandate(board, tid, kind='human')
    board.execute("UPDATE task_events SET created_at=created_at+50 WHERE task_id=? AND kind='commented'", (tid,))
    board.commit()
    assert budget._window_start(board, tid, 2999999999) == since
    assert renew(board, tid, episode, note, writer=HUMAN)['authority_kind'] == 'human'
    assert decision.restricted_since(board, tid) is None
    assert budget._window_start(board, tid, 2999999999) == since


def running_creator(board, tid):
    creator = kb.create_task(board, title='creator', assignee='default')
    board.execute("UPDATE task_events SET payload=json_set(COALESCE(payload,'{}'),'$.creator_task_id',?) "
                  "WHERE task_id=? AND kind='created'", (creator, tid))
    kb._append_event(board, creator, 'owner_admitted', {'session_id': 'local-creator'})
    board.execute("UPDATE tasks SET status='running' WHERE id=?", (creator,))
    board.commit()
    return creator, ('agent', 'local-creator', '7:HERMES_KANBAN_WORKER_TASK')


def test_d_unrelated_running_worker_on_its_own_mandate_is_refused(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    other = kb.create_task(board, title='unrelated', assignee='default')
    kb._append_event(board, other, 'owner_admitted', {'session_id': 'local-other'})
    board.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
    board.commit()
    comment = mandate(board, tid, session='local-other')
    refused(board, tid, episode, comment, 'not_scope_authority',
            writer=('agent', 'local-other', '7:HERMES_KANBAN_WORKER_TASK'), env={'HERMES_KANBAN_WORKER_TASK': other})


def test_d_two_task_identities_in_the_ancestry_are_refused(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    creator, writer = running_creator(board, tid)
    comment = mandate(board, tid, session='local-creator')
    with pytest.raises(RenewalRefused) as caught:
        cli.renew(board, tid, episode=episode, comment_id=comment, reason='x', writer=writer,
                  env={'HERMES_SESSION_ID': 'local-creator', 'HERMES_KANBAN_WORKER_TASK': creator},
                  ancestry=[{'HERMES_SESSION_ID': 'local-creator', 'HERMES_KANBAN_TASK': 't_00000000deadbeef'}])
    assert caught.value.code == 'identity_unavailable'
    assert decision.execution_restricted(board, tid)


def test_d_unreadable_ancestor_above_a_marked_caller_is_refused(board, monkeypatch):
    """The caller's own start-time env names the chat, so the comment writer says agent; only the full walk fails."""
    import os
    from hermes_cli import kanban_comment_writer as writer
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)

    def read(pid):
        if pid == os.getpid():
            return 4242, {'HERMES_SESSION_ID': CHAT}
        raise writer.Inconclusive(f'pid {pid}: environment unreadable')
    monkeypatch.setattr(writer, '_read_proc', read)
    assert writer.cli_comment_writer(env={'HERMES_SESSION_ID': CHAT})[0] == 'agent'
    spoofed(board, tid, episode, comment, 'identity_unavailable', {'HERMES_SESSION_ID': CHAT})


def test_f_creator_task_cannot_renew_on_someone_elses_mandate(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    creator, writer = running_creator(board, tid)
    comment = mandate(board, tid, session='someone-else')
    refused(board, tid, episode, comment, 'mandate_authority', writer=writer, env={'HERMES_KANBAN_WORKER_TASK': creator})


def test_f_owning_chat_cannot_renew_on_a_mandate_a_task_wrote(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    board.execute("UPDATE task_events SET payload=json_set(payload,'$.writer_task','t_00000000deadbeef') "
                  "WHERE task_id=? AND kind='commented'", (tid,))
    board.commit()
    refused(board, tid, episode, comment, 'mandate_authority')


def test_f_mandate_with_two_writer_records_is_refused(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    written = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id DESC LIMIT 1",
                            (tid,)).fetchone()[0]
    kb._append_event(board, tid, 'commented', json.loads(written))
    board.commit()
    refused(board, tid, episode, comment, 'mandate_unattributable')


def test_f_human_mandate_written_on_another_gateway_is_refused(board, monkeypatch):
    from hermes_cli import kanban_origin
    tid, _, episode = stopped(board, monkeypatch)
    note = mandate(board, tid, kind='human')
    board.execute("UPDATE task_events SET origin_gateway='another-gateway' WHERE task_id=? AND kind='commented'", (tid,))
    board.commit()
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('this-gateway', set()))
    refused(board, tid, episode, note, 'mandate_authority', writer=HUMAN)
    board.execute("UPDATE task_events SET origin_gateway='this-gateway' WHERE task_id=? AND kind='commented'", (tid,))
    board.commit()
    assert renew(board, tid, episode, note, writer=HUMAN)['authority_kind'] == 'human'


@pytest.mark.parametrize('change', ["claim_lock='x'", "status='ready'"])
def test_f_claimed_or_non_triage_card_is_refused(board, monkeypatch, change):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    board.execute(f"UPDATE tasks SET {change} WHERE id=?", (tid,))
    board.commit()
    refused(board, tid, episode, comment, 'not_stopped')


def forged_renewal_covering(board, tid, episode, comment, extra=()):
    """A ``scope_renewed`` row that lists every restriction on the card, as a wrong Board answer would."""
    rows = [dict(r) for r in board.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind IN "
        "('spend_budget_triaged','decision_hold_restricted','decision_retained')", (tid,))]
    row = dict(board.execute("SELECT * FROM task_comments WHERE id=?", (comment,)).fetchone())
    kb._append_event(board, tid, 'scope_renewed', {
        'version': 1, 'episode': episode, 'mandate_comment_uid': renewal.comment_ref(row),
        'mandate_body_sha256': renewal.digest(row['body']),
        'covered_restriction_uids': [renewal.key(r) for r in rows] + list(extra)})
    kb._append_event(board, tid, 'promoted_manual', {})  # recorded after the renewal: decides a scope-only stop
    board.commit()
    assert renewal.covered(board, tid)


def test_b_renewal_listing_a_safety_annotation_never_hides_it(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal', 'episode': episode})
    board.commit()
    forged_renewal_covering(board, tid, episode, comment)
    assert decision.restricted_since(board, tid) is not None
    stop = decision.pending_episode(board, tid)
    assert stop is not None and renewal.key(stop) == episode
    assert renewal.mandate(board, tid) is None
    assert '## Scope renewal mandate' not in kb.build_worker_context(board, tid)


def test_b_renewal_listing_a_confirmed_safety_receipt_never_hides_it(board, monkeypatch):
    """The receipt is modelled as confirmed (``receipt_restrictions`` returns it); the readers' clauses are real."""
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    kb._append_event(board, tid, 'decision_retained', {'superseded_by_safety_stop': True, 'episode_uid': episode})
    board.commit()
    receipt = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='decision_retained' "
                                 "ORDER BY id DESC LIMIT 1", (tid,)).fetchone())
    monkeypatch.setattr(decision, 'receipt_restrictions', lambda conn, task_id, **kw: [receipt] if task_id == tid else [])
    forged_renewal_covering(board, tid, episode, comment)
    assert renewal.key(receipt) in renewal.covered(board, tid)
    assert decision.restricted_since(board, tid) is not None
    stop = decision.pending_episode(board, tid)
    assert stop is not None and renewal.key(stop) == episode
    assert renewal.mandate(board, tid) is None


# Neighbouring clauses of the same predicates (exact-head review round 4): one case per clause.

def test_f_deleted_mandate_stops_covering(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    renew(board, tid, episode, comment)
    assert not decision.execution_restricted(board, tid)
    board.execute("DELETE FROM task_comments WHERE id=? AND task_id=?", (comment, tid))
    board.commit()
    assert decision.execution_restricted(board, tid)
    assert kb.promote_task(board, tid, actor='fixture', reason='x')[0] is False
    assert renewal.mandate(board, tid) is None


@pytest.mark.parametrize('damage', [{'version': 2}, {'version': None}, {'episode': ''}, {'episode': None},
                                    {'covered_restriction_uids': None}, {'covered_restriction_uids': 'EPISODE'},
                                    {'mandate_comment_uid': ''}, {'mandate_comment_uid': '999999'},
                                    {'mandate_comment_uid': 'ARABIC'}, {'mandate_body_sha256': None}])
def test_f_malformed_renewal_row_covers_nothing(board, monkeypatch, damage):
    tid, _, episode = stopped(board, monkeypatch)
    comment = mandate(board, tid)
    row = dict(board.execute('SELECT * FROM task_comments WHERE id=?', (comment,)).fetchone())
    payload = {'version': 1, 'episode': episode, 'mandate_comment_uid': renewal.comment_ref(row),
               'mandate_body_sha256': renewal.digest(row['body']), 'covered_restriction_uids': [episode]}
    kb._append_event(board, tid, 'scope_renewed', payload)
    board.commit()
    assert renewal.covered(board, tid) == {episode}, 'the undamaged row is a valid renewal'
    arabic = str(comment).translate(str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩'))  # int() reads it as the same id
    damage = {k: {'EPISODE': episode, 'ARABIC': arabic}.get(v, v) if isinstance(v, str) else v for k, v in damage.items()}
    board.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='scope_renewed'",
                  (json.dumps({**payload, **damage}), tid))
    board.commit()
    assert not renewal.covered(board, tid) and not renewal.renewals(board, tid)
    assert decision.execution_restricted(board, tid)
    assert renewal.key(decision.pending_episode(board, tid)) == episode
    assert renewal.mandate(board, tid) is None


def test_f_renewal_row_with_an_unparseable_payload_covers_nothing(board, monkeypatch):
    tid, _, _ = stopped(board, monkeypatch)
    for raw in ('not json', '[1]', None):
        board.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,1)",
                      (tid, 'scope_renewed', raw))
    board.commit()
    assert not renewal.renewals(board, tid)
    assert decision.execution_restricted(board, tid)


def test_f_renewal_naming_another_cards_comment_by_number_covers_nothing(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    other = kb.create_task(board, title='other', assignee='default')
    row = dict(board.execute('SELECT * FROM task_comments WHERE id=?', (mandate(board, other),)).fetchone())
    kb._append_event(board, tid, 'scope_renewed', {
        'version': 1, 'episode': episode, 'mandate_comment_uid': str(row['id']),
        'mandate_body_sha256': renewal.digest(row['body']), 'covered_restriction_uids': [episode]})
    board.commit()
    assert not renewal.covered(board, tid)
    assert decision.execution_restricted(board, tid)


def test_b_restriction_starts_at_the_earliest_evidence_a_renewal_does_not_cover(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    renew(board, tid, episode, mandate(board, tid))
    assert decision.restricted_since(board, tid) is None
    for at, cause in ((7000, 'safety_refusal'), (5000, 'scope_stopped'), (9000, 'safety_refusal')):
        board.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                      (tid, 'decision_hold_restricted', json.dumps({'cause': cause, 'episode': episode}), at))
    board.commit()
    assert decision.restricted_since(board, tid) == 5000


def test_g_each_stop_needs_its_own_renewal_and_the_newest_mandate_is_the_scope(board, monkeypatch):
    tid, _, first = stopped(board, monkeypatch)
    kb._append_event(board, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': 'again'})
    board.commit()
    second = renewal.key(decision.pending_episode(board, tid))
    assert second != first
    kb._append_event(board, tid, 'promoted_manual', {})  # a refused promote attempt: it decides neither stop
    board.commit()
    link(board, tid)
    payload = renew(board, tid, second, mandate(board, tid, body='Scope for the second stop.'))
    assert payload['covered_restriction_uids'] == [second]
    # Renewing the current stop leaves the older, unrenewed stop restrictive.
    assert decision.execution_restricted(board, tid)
    assert renewal.key(decision.pending_episode(board, tid)) == first
    assert kb.promote_task(board, tid, actor='fixture', reason='x')[0] is False
    assert renewal.mandate(board, tid) is None
    payload = renew(board, tid, first, mandate(board, tid, body='Scope for the first stop.'))
    assert payload['covered_restriction_uids'] == [first]
    assert not decision.execution_restricted(board, tid)
    # Both are renewed: the stop that waits for its decision is the one renewed last, under the newest mandate.
    assert renewal.key(decision.pending_episode(board, tid)) == first
    assert renewal.mandate(board, tid)['body'] == 'Scope for the first stop.'
    kb._append_event(board, tid, 'promoted_manual', {})
    assert not decision.pending(board, tid)


def test_f_renewal_covers_only_scope_cause_evidence_of_its_episode(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'budget', 'episode': episode})
    kb._append_event(board, tid, 'decision_hold_restricted', {'episode': episode})
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': 'another-episode'})
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': episode})
    board.commit()
    bound = renewal.key(dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='decision_hold_restricted' "
                                           "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()))
    assert renew(board, tid, episode, mandate(board, tid))['covered_restriction_uids'] == sorted([episode, bound])
    assert decision.execution_restricted(board, tid), "another episode's scope annotation is not covered"


def test_f_reason_is_required_and_bounded(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    refused(board, tid, episode, comment, 'invalid_reason', reason='x' * 2001)
    refused(board, tid, episode, comment, 'invalid_reason', reason='')
    refused(board, tid, episode, comment, 'invalid_reason', reason=None)
    assert renew(board, tid, episode, comment, reason=' ' + 'x' * 2000 + ' ')['reason'] == 'x' * 2000


def test_f_mandate_bounds_are_on_the_trimmed_body(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    blank = mandate(board, tid)
    board.execute("UPDATE task_comments SET body=' \n ' WHERE id=?", (blank,))
    board.commit()
    refused(board, tid, episode, blank, 'mandate_missing')
    padded = mandate(board, tid)  # 8000 characters between stored whitespace: the bound is on the trimmed body
    board.execute("UPDATE task_comments SET body=? WHERE id=?", (' ' + 'x' * 8000 + ' ', padded))
    board.commit()
    payload = renew(board, tid, episode, padded)
    assert payload['mandate_body_sha256'] == renewal.digest(' ' + 'x' * 8000 + ' ')
    assert not decision.execution_restricted(board, tid), 'the digest is of the body as stored'


def test_f_missing_task_or_card_without_a_stop_is_refused(board, monkeypatch):
    _, _, episode = stopped(board, monkeypatch)
    with pytest.raises(RenewalRefused) as caught:
        renew(board, 't_00000000deadbeef', episode, 1, writer=HUMAN)
    assert caught.value.code == 'no_such_task'
    plain = kb.create_task(board, title='never stopped', assignee='default')
    assert renewal.mandate(board, plain) is None and '## Scope renewal mandate' not in kb.build_worker_context(board, plain)
    board.execute("UPDATE tasks SET status='triage' WHERE id=?", (plain,))
    board.commit()
    with pytest.raises(RenewalRefused) as caught:
        renew(board, plain, episode, mandate(board, plain, kind='human'), writer=HUMAN)
    assert caught.value.code == 'episode_mismatch'
    assert not board.execute("SELECT 1 FROM task_events WHERE kind='scope_renewed'").fetchone()


def test_f_scope_annotation_without_its_stop_is_not_a_renewable_episode(board, monkeypatch):
    tid, _, _ = support.bind(board, monkeypatch)
    board.execute("UPDATE tasks SET status='triage',claim_lock=NULL WHERE id=?", (tid,))
    kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'scope_stopped', 'episode': 'ghost-stop'})
    board.commit()
    orphan = decision.pending_episode(board, tid)
    assert orphan['kind'] == 'decision_hold_restricted'
    for episode in (renewal.key(orphan), 'ghost-stop'):
        refused(board, tid, episode, mandate(board, tid, kind='human'), 'episode_mismatch', writer=HUMAN)


def test_f_agent_authority_never_renews_on_a_comment_recorded_as_human_written(board, monkeypatch):
    """The writer kind is checked on its own: here the session still matches the renewing chat."""
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    board.execute("UPDATE task_events SET payload=json_set(payload,'$.writer_kind','human') "
                  "WHERE task_id=? AND kind='commented'", (tid,))
    board.commit()
    refused(board, tid, episode, comment, 'mandate_authority')


def test_f_chat_authority_without_a_session_matches_no_mandate(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    comment = kb.add_comment(board, tid, 'default', 'Renewed scope.', writer_kind='agent')
    actor = renewal.Actor('agent', None, 'chat', None, '', 'default')
    with pytest.raises(RenewalRefused) as caught:
        renewal.renew_local(board, tid, episode=episode, comment_id=comment, reason='x', actor=actor)
    assert caught.value.code == 'mandate_authority'


def test_f_creator_task_renews_on_a_mandate_its_own_run_wrote_under_another_session(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    creator, writer = running_creator(board, tid)
    comment = mandate(board, tid, session='local-earlier-run')
    board.execute("UPDATE task_events SET payload=json_set(payload,'$.writer_task',?) WHERE task_id=? AND kind='commented'",
                  (creator, tid))
    board.commit()
    payload = renew(board, tid, episode, comment, writer=writer, env={'HERMES_KANBAN_WORKER_TASK': creator})
    assert payload['authority_kind'] == 'creator_task' and payload['actor_session'] == 'local-creator'


def test_d_writer_that_is_neither_human_nor_agent_is_refused(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    refused(board, tid, episode, mandate(board, tid), 'identity_unavailable', writer=('service', CHAT, '4242:HERMES_SESSION_ID'))


def test_d_creator_task_whose_row_is_gone_is_refused(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    creator, writer = running_creator(board, tid)
    comment = mandate(board, tid, session='local-creator')
    board.execute("PRAGMA foreign_keys=OFF")
    board.execute("DELETE FROM tasks WHERE id=?", (creator,))
    board.commit()
    assert renewal._creator_sessions(board, creator) == {'local-creator'}, 'its admission events remain'
    refused(board, tid, episode, comment, 'not_scope_authority', writer=writer, env={'HERMES_KANBAN_WORKER_TASK': creator})


def test_d_session_named_only_by_the_ssh_marker_counts_in_the_ancestry(board, monkeypatch):
    tid, _, episode = stopped(board, monkeypatch)
    session = 'local-' + 'b' * 64
    kb._append_event(board, tid, 'owner_admitted', {'session_id': session})
    board.commit()
    link(board, tid)
    comment = mandate(board, tid)
    with pytest.raises(RenewalRefused) as caught:  # the card's own run, relayed over ssh
        cli.renew(board, tid, episode=episode, comment_id=comment, reason='x', writer=AGENT,
                  env={'HERMES_SESSION_ID': CHAT}, ancestry=[{'LC_HERMES_AGENT': session}])
    assert caught.value.code == 'own_run'
    with pytest.raises(RenewalRefused) as caught:  # another chat's session
        cli.renew(board, tid, episode=episode, comment_id=comment, reason='x', writer=AGENT,
                  env={'HERMES_SESSION_ID': CHAT}, ancestry=[{'LC_HERMES_AGENT': '20261101_000000_abcdef'}])
    assert caught.value.code == 'identity_unavailable'
    assert decision.execution_restricted(board, tid)


def test_a_human_operator_needs_no_readable_ancestry(board, monkeypatch):
    from hermes_cli import kanban_comment_writer as writer
    tid, _, episode = stopped(board, monkeypatch)

    def unreadable(pid):
        raise writer.Inconclusive('pid 7: environment unreadable')
    monkeypatch.setattr(writer, '_read_proc', unreadable)
    payload = cli.renew(board, tid, episode=episode, comment_id=mandate(board, tid, kind='human'), reason='x',
                        writer=HUMAN, env={})
    assert payload['authority_kind'] == 'human' and payload['actor_session'] is None


def test_ancestry_environs_reads_every_ancestor_and_refuses_what_it_cannot_finish():
    from hermes_cli import kanban_comment_writer as writer
    tree = {10: (9, {'A': '1'}), 9: (8, {}), 8: (1, {'B': '2'}), 1: (0, {'INIT': '1'})}
    assert writer.ancestry_environs(10, read=tree.__getitem__) == [{'A': '1'}, {'B': '2'}, {'INIT': '1'}]
    assert writer.ancestry_environs(8, read={8: (0, {'B': '2'})}.__getitem__) == [{'B': '2'}]
    assert writer.ancestry_environs(1, read={1: (7, {'INIT': '1'})}.__getitem__) == [{'INIT': '1'}], 'init ends the walk'
    with pytest.raises(writer.Inconclusive, match='loop'):
        writer.ancestry_environs(10, read={10: (9, {'A': '1'}), 9: (10, {})}.__getitem__)
    with pytest.raises(writer.Inconclusive, match='deeper'):
        writer.ancestry_environs(10_000, read=lambda pid: (pid - 1, {}))
    chain = {pid: (pid - 1, {'N': str(pid)}) for pid in range(2, writer.MAX_ANCESTRY + 1)} | {1: (0, {})}
    assert len(writer.ancestry_environs(writer.MAX_ANCESTRY, read=chain.__getitem__)) == writer.MAX_ANCESTRY - 1


def test_h_gateway_notice_names_the_card_in_the_renewal_route():
    from types import SimpleNamespace
    from gateway.kanban_watchers_notifier import _fmt_spend_budget
    text, body, _ = _fmt_spend_budget(SimpleNamespace(payload={'cause': 'scope_stopped'}),
                                      SimpleNamespace(head='Kanban t_1', task_id='t_00000000deadbeef'))
    assert 'renew-scope t_00000000deadbeef --episode' in text and 'renew-scope t_00000000deadbeef --episode' in body
    assert '<task>' in decision.notice({'cause': 'scope_stopped'})


def test_j_the_command_line_prints_the_next_step_in_text_mode(board, monkeypatch, capsys):
    tid, _, episode = stopped(board, monkeypatch)
    link(board, tid)
    comment = mandate(board, tid)
    ancestry(monkeypatch, {'HERMES_SESSION_ID': CHAT, 'HERMES_AGENT': '1'}, {'HERMES_SESSION_ID': CHAT}, {})
    monkeypatch.setenv('HERMES_SESSION_ID', CHAT)
    assert argv(tid, '--episode', 'not-the-episode', '--mandate-comment', str(comment), '--reason', 'x') == 1
    assert 'renew-scope refused (episode_mismatch)' in capsys.readouterr().out
    assert argv(tid, '--episode', episode, '--mandate-comment', str(comment), '--reason', 'x') == 0
    out = capsys.readouterr().out
    assert f'Scope renewed on {tid} for stop {episode}' in out and f'hermes kanban promote {tid}' in out
    assert not out.lstrip().startswith('{')
