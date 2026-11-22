"""Scope renewal against really confirmed retain receipts: no reader is patched.

The unsynced ``board`` fixture has no ``kanban_failure_commands`` table, so ``receipt_restrictions`` is empty
there. Here a receipt is confirmed the way the transport confirms it, and each case fails if the one clause
that reads it is dropped.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_refusal_receipt as refusal
from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_retain_receipt as support

live = support.live
STOP = 'fixture-stop'
HUMAN = renewal.Actor('human', None, 'human', None, '', 'default')
BODY = 'Renewed scope: one round.'


def confirmed(live, *, safety):
    """A triaged card with one scope stop and one locally confirmed receipt bound to another episode uid."""
    owned, receipt, _, _hub = support.fixture(live)
    receipt['superseded_by_safety_stop'] = safety
    tid = owned['task_id']
    rid = live.execute('SELECT id FROM task_runs WHERE uid=?', (owned['run_uid'],)).fetchone()[0]
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status='triage',claim_lock=NULL WHERE id=?", (tid,))
        kb._append_event(live, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': 'out of scope'})
        live.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='spend_budget_triaged'", (STOP, tid))
        kb._append_event(live, tid, 'decision_retained', receipt, run_id=rid)
        live.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='decision_retained'", (owned['event_uid'], tid))
        live.execute("UPDATE kanban_failure_commands SET delivery_state='confirmed',canonical_receipt_json=? WHERE run_uid=?",
                     (json.dumps(receipt), owned['run_uid']))
    assert [e['uid'] for e in decision.receipt_restrictions(live, tid)] == [owned['event_uid']]
    return tid, owned['event_uid']


def forged(live, tid, uids, *, ref=None, promote=False):
    """A ``scope_renewed`` row covering ``uids``, as a Board answer would project it."""
    comment = kb.add_comment(live, tid, 'default', BODY, writer_kind='agent', writer_session='chat')
    row = dict(live.execute('SELECT * FROM task_comments WHERE id=?', (comment,)).fetchone())
    with kb.write_txn(live):
        kb._append_event(live, tid, 'scope_renewed', {
            'version': 1, 'episode': STOP, 'mandate_comment_uid': ref(row) if ref else renewal.comment_ref(row),
            'mandate_body_sha256': renewal.digest(row['body']), 'covered_restriction_uids': list(uids)})
        if promote:
            kb._append_event(live, tid, 'promoted_manual', {})
    return row


def human_mandate(live, tid):
    return kb.add_comment(live, tid, 'default', BODY, writer_kind='human')


def test_confirmed_safety_receipt_listed_by_a_renewal_stays_restrictive(live):
    tid, receipt_uid = confirmed(live, safety=True)
    assert refusal.restricted(live, tid)
    assert {renewal.key(e) for e in renewal.scope_evidence(live, tid, STOP)} == {STOP}
    forged(live, tid, [STOP, receipt_uid], promote=True)
    assert {STOP, receipt_uid} <= renewal.covered(live, tid)
    assert decision.restricted_since(live, tid) is not None
    stop = decision.pending_episode(live, tid)
    assert stop is not None and renewal.key(stop) == STOP
    assert renewal.mandate(live, tid) is None
    assert 'safety' in decision.execution_refusal(live, tid).lower()


def test_confirmed_scope_receipt_is_evidence_and_stays_restrictive_until_covered(live):
    tid, receipt_uid = confirmed(live, safety=False)
    assert not refusal.restricted(live, tid)
    assert {renewal.key(e) for e in renewal.scope_evidence(live, tid, STOP)} == {STOP, receipt_uid}
    forged(live, tid, [STOP])  # the stop alone: the confirmed scope receipt is still uncovered
    assert renewal.covered(live, tid) == {STOP}
    assert decision.execution_restricted(live, tid)
    stop = decision.pending_episode(live, tid)
    assert stop is not None and renewal.key(stop) == STOP
    assert renewal.mandate(live, tid) is None


def test_covered_confirmed_scope_receipt_releases_only_after_a_later_promotion(live):
    tid, receipt_uid = confirmed(live, safety=False)
    forged(live, tid, [STOP, receipt_uid])
    assert not decision.execution_restricted(live, tid)
    assert decision.pending(live, tid)
    assert renewal.mandate(live, tid)['body'] == BODY
    with kb.write_txn(live):
        kb._append_event(live, tid, 'promoted_manual', {})
    assert not decision.pending(live, tid)


def test_renewal_freezes_the_confirmed_scope_receipt_with_its_stop(live):
    tid, receipt_uid = confirmed(live, safety=False)
    payload = renewal.renew_local(live, tid, episode=STOP, comment_id=human_mandate(live, tid), reason='ok', actor=HUMAN)
    assert payload['covered_restriction_uids'] == sorted([STOP, receipt_uid])
    assert not decision.execution_restricted(live, tid) and decision.pending(live, tid)


def test_confirmed_safety_receipt_refuses_the_renewal(live):
    tid, _ = confirmed(live, safety=True)
    with pytest.raises(renewal.RenewalRefused) as caught:
        renewal.renew_local(live, tid, episode=STOP, comment_id=human_mandate(live, tid), reason='ok', actor=HUMAN)
    assert caught.value.code == 'safety_restricted'
    assert not live.execute("SELECT 1 FROM task_events WHERE kind='scope_renewed'").fetchone()


def test_renewing_the_current_stop_leaves_another_episodes_confirmed_receipt_restrictive(live):
    tid, _ = confirmed(live, safety=False)
    with kb.write_txn(live):
        live.execute("UPDATE task_events SET created_at=created_at-100 WHERE task_id=?", (tid,))
        kb._append_event(live, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': 'again'})
        live.execute("UPDATE task_events SET created_at=created_at-50, uid='other-stop' WHERE task_id=? "
                     "AND kind='spend_budget_triaged' AND uid!=?", (tid, STOP))
    assert renewal.key(decision.pending_episode(live, tid)) == 'other-stop'
    payload = renewal.renew_local(live, tid, episode='other-stop', comment_id=human_mandate(live, tid), reason='ok',
                                  actor=HUMAN)
    assert payload['covered_restriction_uids'] == ['other-stop']
    assert decision.execution_restricted(live, tid)
    assert renewal.key(decision.pending_episode(live, tid)) == STOP


def test_numeric_mandate_ref_never_matches_a_comment_that_has_its_own_uid(live):
    tid, receipt_uid = confirmed(live, safety=False)
    row = forged(live, tid, [STOP, receipt_uid], ref=lambda row: str(row['id']))
    assert row['uid'] and row['uid'] != str(row['id'])
    assert not renewal.covered(live, tid)
    assert decision.execution_restricted(live, tid)


def test_mandate_named_by_a_uid_of_another_card_covers_nothing(live):
    """On a synced board the mandate is found by uid, and only on the renewed card."""
    tid, receipt_uid = confirmed(live, safety=False)
    other = kb.create_task(live, title='other', assignee='default')
    comment = kb.add_comment(live, other, 'default', BODY, writer_kind='agent', writer_session='chat')
    row = dict(live.execute('SELECT * FROM task_comments WHERE id=?', (comment,)).fetchone())
    with kb.write_txn(live):
        kb._append_event(live, tid, 'scope_renewed', {
            'version': 1, 'episode': STOP, 'mandate_comment_uid': row['uid'],
            'mandate_body_sha256': renewal.digest(row['body']), 'covered_restriction_uids': [STOP, receipt_uid]})
    assert not renewal.covered(live, tid)
    assert decision.execution_restricted(live, tid)


def test_synced_mandate_is_bound_to_its_writer_record_by_uid(live):
    """A second ``commented`` row that names only the local id attests nothing about a comment that has a uid."""
    tid, _ = confirmed(live, safety=False)
    comment = human_mandate(live, tid)
    with kb.write_txn(live):
        kb._append_event(live, tid, 'commented', {'comment_id': comment, 'writer_kind': 'agent', 'writer_session': 'x'})
    payload = renewal.renew_local(live, tid, episode=STOP, comment_id=comment, reason='ok', actor=HUMAN)
    written = live.execute("SELECT uid, payload FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id",
                           (tid,)).fetchall()
    assert payload['mandate_event_uid'] == written[0][0] and json.loads(written[0][1])['writer_kind'] == 'human'
