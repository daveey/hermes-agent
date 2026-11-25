"""History contracts on disposable board/source databases; never live history."""
import copy
import json

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


@pytest.mark.parametrize('field,value', [('request_id', 'wrong'), ('scope', 'wrong')])
def test_offer_binding_independent_of_hash(home, operation, field, value):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        evidence['snapshot']['ask'][sx.STAMP][field] = value
        offer = sx._own(conn, sx.OFFERED)[0]
        payload = copy.deepcopy(offer['payload'])
        payload[sx.STAMP] = evidence['snapshot']['ask'][sx.STAMP]
        payload['ask_sha256'] = source.digest(evidence['snapshot']['ask'])
        with h.kb.write_txn(conn):
            conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), offer['id']))
        with pytest.raises(sx.Refused, match='^local_offer_replaced_or_invalid$'):
            local._offer(conn, evidence)


@pytest.mark.parametrize('status', ['archived', 'done'])
def test_closed_owner_without_archive_event(home, operation, status):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        with h.kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, home['task']))
        with pytest.raises(sx.Refused, match='^local_owner_held_or_closed$'):
            local.eligible(conn, evidence)


def test_replaced_current_ask_with_same_offer(home, operation):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        changed = copy.deepcopy(evidence['snapshot']['ask'])
        changed['action'] += ' replacement'
        with h.kb.write_txn(conn):
            conn.execute('UPDATE tasks SET human_request=? WHERE id=?', (json.dumps(changed), home['task']))
        with pytest.raises(sx.Refused, match='^local_ask_replaced$'):
            local.eligible(conn, evidence)


def test_historical_receipt_is_not_just_operation_id(home, operation):
    with source.custody(local.RECOVERY_OPERATION) as evidence, kbc.connect_closing() as conn:
        event = sx._own(conn, 'unblocked')[0]
        payload = copy.deepcopy(event['payload'])
        payload['user_steps_projection']['fingerprint'] = 'wrong'
        with h.kb.write_txn(conn):
            conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event['id']))
        with pytest.raises(sx.Refused, match='^local_unblock_receipt_changed$'):
            local._prior_unblock(conn, evidence)


def test_validate_refuses_changed_grant(home, operation):
    grant = h.recover(home)
    grant['fingerprint'] = 'wrong'
    with kbc.connect_closing() as conn:
        with pytest.raises(sx.Refused, match='^local_grant_changed$'):
            local.validate(conn, grant, now=int(local.time.time()))
        assert sx._own(conn, sx.SPENT) == []


def test_historical_replay_does_not_require_current_offer(home, operation):
    grant = h.recover(home)
    with kbc.connect_closing() as conn:
        offer = sx._own(conn, sx.OFFERED)[0]
        replacement = copy.deepcopy(offer['payload'])
        replacement[sx.STAMP]['offer_uid'] = 'replacement'
        with h.kb.write_txn(conn):
            h.kb._append_event(conn, home['task'], sx.OFFERED, replacement)
        assert local.recorded(conn, grant['offer_uid']) == grant
        with pytest.raises(sx.Refused, match='^local_offer_replaced_or_invalid$'):
            local.validate(conn, grant, now=int(local.time.time()))
