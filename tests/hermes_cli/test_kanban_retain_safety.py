"""Release-first recovery is exact canonical safety evidence, never replacement intent."""
import json

import pytest
from hermes_cli import kanban_retain_transport as transport
from tests.hermes_cli import test_kanban_retain_receipt as support

live = support.live


@pytest.mark.parametrize('outcome', ['safety_refused', 'failed', 'running'])
def test_release_first_recovers_only_canonical_safety_terminal(live, monkeypatch, outcome):
    from types import SimpleNamespace
    from hermes_cli import kanban_retain as service
    from hermes_cli import kanban_sync_client as client
    owned, _, rows, hub = support.fixture(live)
    monkeypatch.setattr(service.actors, 'bound_actor', lambda conn: SimpleNamespace(task=owned['task_id'], run_uid=owned['run_uid']))
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'Hub', lambda _: hub)
    monkeypatch.setattr(transport, 'project', lambda *args: None)
    live.execute("UPDATE kanban_sync_state SET gateway_id=?", (hub.ident.gateway,))
    hub.close = lambda: None
    def recover():
        return service.retain(live, board='default', **{k:v for k,v in support.support.PAYLOAD.items() if k != 'version'})
    live.execute("UPDATE kanban_failure_commands SET operation_kind='release',request_json=NULL,request_sha256=NULL WHERE run_uid=?", (owned['run_uid'],))
    original = transport.command(live, owned['run_uid'])
    run = rows['task_runs', owned['run_uid']]
    run.update(outcome=outcome, metadata=json.dumps({'cause':'safety_refusal','source_result_event_uid':'source', 'episode':'fixture-stop'}))
    rows['task_events', 'source'] = {'uid':'source', 'task_id':owned['task_id'], 'run_uid':owned['run_uid'],
        'kind':'worker_result','version':1,'origin_gateway':hub.ident.gateway,
        'payload':json.dumps({'failed':True,'failure_reason':'content_policy_blocked','failure_retryable':False})}
    rows['task_events', 'annotation'] = {'uid':'annotation','task_id':owned['task_id'],'run_uid':owned['run_uid'],
        'kind':'decision_hold_restricted','payload':json.dumps({'cause':'safety_refusal','episode':'fixture-stop','source_result_event_uid':'source'})}
    rid = live.execute('SELECT id FROM task_runs WHERE uid=?', (owned['run_uid'],)).fetchone()[0]
    from hermes_cli import kanban_db as kb
    with kb.write_txn(live):
        kb._append_event(live, owned['task_id'], 'decision_hold_restricted', {'cause':'safety_refusal'}, run_id=rid)
        live.execute("UPDATE task_events SET uid='annotation' WHERE task_id=? AND kind='decision_hold_restricted'", (owned['task_id'],))
    if outcome != 'safety_refused':
        with pytest.raises(ValueError):
            recover()
        assert transport.command(live, owned['run_uid']) == original
        return
    result = recover()
    assert result['terminal_for_run'] is True and result['disposition'] == 'superseded_by_safety_stop'
    assert not hub.calls
    after = transport.command(live, owned['run_uid'])
    assert after['op_id'] == original['op_id'] and after['operation_kind'] == 'release'
    assert after['request_json'] is None and after['request_sha256'] is None
