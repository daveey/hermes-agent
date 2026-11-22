"""A reserved event name is not attestation of historical or ACK-only evidence."""
import json

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_refusal_receipt as refusal
from tests.hermes_cli import test_kanban_retain_receipt as support

live = support.live


@pytest.mark.parametrize('state', ['unknown', 'acknowledged', 'confirmed_bad_uid', 'confirmed_bad_payload'])
def test_receipt_restriction_requires_exact_independently_confirmed_custody(live, state):
    owned, receipt, _, _hub = support.fixture(live)
    receipt['superseded_by_safety_stop'] = True
    rid = live.execute('SELECT id FROM task_runs WHERE uid=?', (owned['run_uid'],)).fetchone()[0]
    with kb.write_txn(live):
        kb._append_event(live, owned['task_id'], 'decision_retained', receipt, run_id=rid)
        live.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='decision_retained'", (owned['event_uid'], owned['task_id']))
        live.execute('UPDATE kanban_failure_commands SET delivery_state=?,canonical_receipt_json=? WHERE run_uid=?',
                     ('confirmed' if state.startswith('confirmed') else state,json.dumps(receipt),owned['run_uid']))
        if state == 'confirmed_bad_uid':
            live.execute("UPDATE task_events SET uid='historical-lookalike' WHERE kind='decision_retained'")
        if state == 'confirmed_bad_payload':
            live.execute("UPDATE task_events SET payload='{}' WHERE kind='decision_retained'")
    assert not decision.execution_restricted(live, owned['task_id'])
    assert not refusal.restricted(live, owned['task_id'])
    assert not decision.pending(live, owned['task_id'])
