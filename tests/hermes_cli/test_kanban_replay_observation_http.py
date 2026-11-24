"""Real HTTP observation refusals do not fail already ACKed transport."""
import pytest

from hermes_cli import kanban_replay_deferred as deferred
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_observation as observation
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_deferred as helpers
from tests.hermes_cli import test_kanban_replay_observation as reads
from tests.hermes_cli.test_kanban_replay_http_session import serving

live = helpers.live


@pytest.mark.parametrize('status', [410, 403, 503])
def test_observation_http_refusal_releases_token_keeps_membership_and_transport(live, status, caplog):
    task = helpers.intent(live)
    reads.seed(live, task=task)
    receiver = reads.Observing()
    receiver.entries = [reads.entry('captured-origin')]
    assert reads.poll(live, receiver)['status'] == 'observed'
    prior = observation.observed(live)
    state = journal.snapshot(live)
    receiver.slot.update(h='1', a='1', last_op_id=state['last_op_id'],
                         last_payload_sha256=state['last_payload_sha256'], last_verdict=state['last_verdict'])

    def response_status(path):
        return status if '/deferred?task=' in path else 200

    with serving(live, receiver, response_status=response_status) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            result = client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
            assert result.deferred == 1
            assert receiver.slot['a'] == '2' and journal.pending_wire(live) is None
            assert observation.observed(live) == prior, 'HTTP failure is not empty membership'
            pending = deferred.pending(live)
            assert pending and any(row['origin'] == 'captured-origin' for row in pending)
            assert live.execute('SELECT COUNT(*) FROM kanban_replay_observation_reads1').fetchone()[0] == 0
            assert 'Deferred observation unavailable' in caplog.text
            assert '/deferred?task=' in requests[-1][1]
            # The origin lifecycle event remains transportable despite the read failure.
            original = receiver._call

            def ordinary_event(method, path, body=None):
                if path.endswith('/ops'):
                    return helpers.helpers.Receiver._call(receiver, method, path, body)
                return original(method, path, body)

            receiver._call = ordinary_event
            assert client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1).applied == 1
            assert receiver.slot['a'] == '3' and receiver.sent[-1]['table'] == 'task_events'
            assert deferred.pending(live) == pending
            assert live.execute('SELECT COUNT(*) FROM kanban_replay_observation_reads1').fetchone()[0] == 0
        finally:
            hub.close()
