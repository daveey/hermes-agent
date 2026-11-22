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
    return cli.renew(board, tid, episode=episode, comment_id=comment, reason=reason, writer=writer, env=env or {})


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


def test_h_notice_and_unblock_text_name_the_route_only_for_scope_stops(board, monkeypatch):
    tid, _, _ = stopped(board, monkeypatch)
    text = decision.unblock_refusal(board, tid)
    assert f'hermes kanban renew-scope {tid} --episode' in text and f'hermes kanban promote {tid}' in text
    notice = decision.notice({'cause': 'scope_stopped', 'task_id': tid})
    assert f'hermes kanban renew-scope {tid} --episode' in notice and 'owning chat' in notice
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
