"""Historical receipt contracts on disposable fixture databases only."""
import copy

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_selfwork_exception as sx
from hermes_cli import kanban_selfwork_local as local
from hermes_cli import kanban_selfwork_local_source as source
from tests.hermes_cli import test_kanban_selfwork_local as h

world = h.world
nochat_envelope = h.nochat_envelope
request_envelope = h.request_envelope
home = h.home
operation = h.operation


@pytest.mark.parametrize('case', ['missing', 'empty', 'valid'])
def test_event_uid_requires_present_nonempty_identity(home, operation, case):
    with kbc.connect_closing() as conn:
        event = sx._own(conn, 'unblocked')[0]
        expected = local._uid(conn, event)
        if case == 'missing':
            event = dict(event, id=-1)
        elif case == 'empty':
            with h.kb.write_txn(conn):
                conn.execute("UPDATE task_events SET uid='' WHERE id=?", (event['id'],))
        if case == 'valid':
            assert local._uid(conn, event) == expected
        else:
            with pytest.raises(sx.Refused, match='^local_event_identity_missing$'):
                local._uid(conn, event)


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'before_offer', 'wrong_comment_id', 'foreign_operation', 'unblock_before_offer'])
def test_prior_unblock_requires_exact_comment_order(home, operation, monkeypatch, case):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        own = sx._own
        offer = own(conn, sx.OFFERED)[0]
        comments = copy.deepcopy(own(conn, 'commented'))
        unblocks = copy.deepcopy(own(conn, 'unblocked'))
        # Preserve unrelated comments; alter only the event that authenticates this decision.
        decision = conn.execute("SELECT id FROM task_comments WHERE task_id=? AND author='user-steps'", (home['task'],)).fetchone()[0]
        matching = [e for e in comments if e['payload'].get('comment_id') == decision]
        assert len(matching) == 1
        # unblock_before_offer checks an early refusal reason; the later
        # comment-order guard independently refuses, so it is not a bypass kill.
        mutations = {
            'missing': lambda: comments.remove(matching[0]),
            'duplicate': lambda: comments.append(copy.deepcopy(matching[0])),
            'before_offer': lambda: matching[0].update(id=offer['id']),
            'unblock_before_offer': lambda: unblocks[0].update(id=offer['id']),
            'wrong_comment_id': lambda: matching[0]['payload'].update(comment_id=-1),
            'foreign_operation': lambda: unblocks[0]['payload']['user_steps_projection'].update(operationId='foreign'),
        }
        mutations[case]()
        def filtered(c, kind):
            return comments if kind == 'commented' else unblocks if kind == 'unblocked' else own(c, kind)
        monkeypatch.setattr(sx, '_own', filtered)
        reason = 'local_unblock_missing_or_ambiguous' if case in ('foreign_operation', 'unblock_before_offer') else 'local_comment_event_invalid'
        with pytest.raises(sx.Refused, match='^' + reason + '$'):
            local._prior_unblock(conn, evidence)


@pytest.mark.parametrize('case', ['duplicate', 'missing_offer', 'wrong_ask_hash', 'wrong_stamp', 'unbound_stamp', 'grant_changed', 'before_unblock', 'wrong_version'])
def test_recorded_receipt_requires_unique_bound_history(home, operation, monkeypatch, case):
    grant = h.recover(home)
    with kbc.connect_closing() as conn:
        own = sx._own
        recovered = copy.deepcopy(own(conn, local.RECOVERED))
        offers = copy.deepcopy(own(conn, sx.OFFERED))
        def unbound_stamp():
            offers[0]['payload'][sx.STAMP]['request_id'] = 'wrong'
            recovered[0]['payload'][sx.GRANT]['stamp'] = copy.deepcopy(offers[0]['payload'][sx.STAMP])
        mutations = {
            'duplicate': lambda: recovered.append(copy.deepcopy(recovered[0])),
            'missing_offer': offers.clear,
            'wrong_ask_hash': lambda: offers[0]['payload'].update(ask_sha256='wrong'),
            'wrong_stamp': lambda: offers[0]['payload'][sx.STAMP].update(offer_uid='other'),
            'unbound_stamp': unbound_stamp,
            'grant_changed': lambda: recovered[0]['payload'][sx.GRANT].update(fingerprint='wrong'),
            'before_unblock': lambda: recovered[0].update(id=own(conn, 'unblocked')[0]['id'] - 1),
            'wrong_version': lambda: recovered[0]['payload'][sx.GRANT].update(local_version=0),
        }
        mutations[case]()
        def filtered(c, kind):
            return recovered if kind == local.RECOVERED else offers if kind == sx.OFFERED else own(c, kind)
        monkeypatch.setattr(sx, '_own', filtered)
        if case == 'wrong_version':
            assert local.recorded(conn, grant['offer_uid']) is None
        else:
            reason = ('local_grant_ambiguous' if case == 'duplicate' else
                      'local_grant_evidence_changed' if case in ('grant_changed', 'before_unblock') else
                      'local_grant_offer_invalid')
            with pytest.raises(sx.Refused, match='^' + reason + '$'):
                local.recorded(conn, grant['offer_uid'])


@pytest.mark.parametrize('case', ['no_offer', 'no_owner'])
def test_missing_offer_or_owner_has_typed_refusal(home, operation, monkeypatch, case):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        if case == 'no_offer':
            own = sx._own
            monkeypatch.setattr(sx, '_own', lambda c, kind: [] if kind == sx.OFFERED else own(c, kind))
            with pytest.raises(sx.Refused, match='^local_offer_replaced_or_invalid$'):
                local._offer(conn, evidence)
        else:
            # Isolate the second lookup after home validation, modeling disappearance.
            # This is a defensive seam test, not a demonstrated authorization bypass.
            sx._home(conn)
            monkeypatch.setattr(sx, '_home', lambda c: None)
            conn.execute('DELETE FROM tasks WHERE id=?', (home['task'],))
            with pytest.raises(sx.Refused, match='^local_owner_held_or_closed$'):
                local.eligible(conn, evidence)


@pytest.mark.parametrize('case', ['valid', 'different_event'])
def test_ordinary_grant_is_bound_to_its_own_unblock(home, operation, monkeypatch, case):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        own = sx._own
        prior = copy.deepcopy(own(conn, 'unblocked')[0])
        stamp = evidence['snapshot']['ask'][sx.STAMP]
        grant = local._grant(evidence, stamp, None)
        event = copy.deepcopy(prior)
        event['payload'][sx.GRANT] = grant
        if case == 'different_event':
            # The grant event cannot borrow a different earlier plain-unblock receipt.
            event['id'] += 1
            event['payload'].pop('user_steps_projection')
            events = [event, prior]
        else:
            events = [event]
        monkeypatch.setattr(sx, '_own', lambda c, kind: events if kind == 'unblocked' else own(c, kind))
        if case == 'valid':
            assert local.recorded(conn, stamp['offer_uid']) == grant
        else:
            with pytest.raises(sx.Refused, match='^local_grant_evidence_changed$'):
                local.recorded(conn, stamp['offer_uid'])


def test_explicit_eligibility_time_and_absent_current_ask(home, operation):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        stamp = evidence['snapshot']['ask'][sx.STAMP]
        # A plain unblock clears the ask; historical authority may still be evaluated.
        conn.execute('UPDATE tasks SET human_request=? WHERE id=?', (source.encoded({'replacement': True}), home['task']))
        with pytest.raises(sx.Refused, match='^local_ask_replaced$'):
            local.eligible(conn, evidence, now=stamp['expires_at'] - 1)
        conn.execute('UPDATE tasks SET human_request=NULL WHERE id=?', (home['task'],))
        assert local.eligible(conn, evidence, now=stamp['expires_at'] - 1) == stamp
        with pytest.raises(sx.Refused, match='^local_offer_expired_or_spent$'):
            local.eligible(conn, evidence, now=stamp['expires_at'])
