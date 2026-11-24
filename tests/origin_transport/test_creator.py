"""Real courier forwarding and owner creation retain native task ownership."""
import json

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_origin_native as native
from hermes_cli import kanban_placement as placement, kanban_sync_client
from hermes_state import SessionDB
from tests.origin_transport import test_forwarding as support

network = support.network


def test_worker_native_creator_survives_courier_and_owner(network, monkeypatch):
    courier, dbs, service, host, runner, adapter, _ = network
    with host('hub'):
        from hermes_constants import get_hermes_home
        sessions = SessionDB(db_path=get_hermes_home() / 'state.db')
        sessions.create_session('chat-one', 'cli')
        sessions.close()
        with kbc.connect_closing() as conn:
            from hermes_cli import kanban_db_sync as sync
            sync.set_mode(conn, 'primary')
            assert native._primary_scope(conn) == ('hub', 'default', 'default')
            parent = kb.create_task(conn, title='Implementation', session_id='creator')
            creator = {'gateway': 'hub', 'profile': 'default', 'board': 'default', 'task_id': parent}
            origin = {'gateway': 'hub', 'profile': 'default', 'session': 'chat-one'}
            binding = {'binding_id': 'native-' + parent, 'version': 1, 'publication_version': 1,
                       'owner': creator, 'origin': origin, 'task_record_key': 'default/default/' + parent}
            # Controlled durable native receipt, not a fabricated successful adapter response.
            native._install_revocation(conn)
            conn.execute('INSERT INTO origin_native_receipts VALUES (?,?,NULL,1)', (parent, json.dumps(binding)))
            conn.commit()
    monkeypatch.setattr(kanban_sync_client.Hub, 'machines', lambda self: {
        'me': 'hub', 'machines': [{'name': name, 'state': 'active'} for name in ('hub', 'worker-7')]})
    monkeypatch.setattr(kanban_sync_client.Hub, 'snapshot_page', lambda *a, **k: {
        'table': 'leases', 'rows': [{'task_id': parent, 'holder': 'worker-7', 'run_uid': 'run-one',
                                   'expires': 9999999999999}], 'more': False})
    with host('worker-7'), monkeypatch.context() as patch:
        patch.setattr(courier, 'load_config', lambda: {'gateway': 'worker-7', 'hub_url': 'https://example.com'})
        patch.setattr(courier, 'send_queued', lambda db, api: len(service.deliver(db, 'worker-7')))
        patch.setattr(placement, 'hub_carries_options', lambda cli: True)
        patch.setattr(placement, 'remote_state', lambda rid: 'queued')
        import contextlib
        import io

        def schema_runner(cfg, argv, stdin):
            from hermes_cli import kanban_creation_contract
            assert argv == ['--run-module', 'hermes_cli.kanban_creation_contract']
            output = io.StringIO()
            with monkeypatch.context() as schema_patch, contextlib.redirect_stdout(output):
                schema_patch.setattr('sys.stdin', io.StringIO(stdin.decode()))
                rc = kanban_creation_contract.main()
            return rc, output.getvalue(), ''

        def cli(argv):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                try:
                    rc = courier.main(argv[1:], api=service, runner=schema_runner)
                except SystemExit as exc:
                    rc = exc.code
            return rc, output.getvalue(), ''

        patch.setattr(placement, 'hub_runner', cli)
        result = placement.create_remote({'title': 'Independent review', 'assignee': 'default', 'parents': [parent]},
            target='hub', self_gw='worker-7', origin_session='chat-one', origin_profile='default',
            origin_gateway='hub', creator=creator, run_uid='run-one', canonical_home=True,
            cfg={'creator_session': 'worker-session'})
        assert result['ok'], result
    request = next(iter(service.requests.values()))
    with host('hub'):
        courier.journal_request(dbs['hub'], request)
        status, result = courier.advance_origin_forward(dbs['hub'], {}, request, runner, service)
        assert status == 'pending', result
        child = service.deliver(dbs['hub'], 'hub')[0]
        child = service.requests[child['id']]
        assert child['args']['creator'] == creator
        forged = adapter('origin-create', {**child, 'from': 'gw:worker-7'})
        assert forged['status'] == 'rejected'
        courier.journal_request(dbs['hub'], child)
        status, result = courier.advance_origin_create(dbs['hub'], {}, child, runner, service)
        assert status == 'pending' and result.get('created') is True, result
        task_id = result['task_id']
        assert adapter('origin-create', child)['task_id'] == task_id
        with kbc.connect_closing() as conn:
            row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created'", (task_id,)).fetchone()
            assert json.loads(row[0])['creator_task_id'] == parent
            task = kb.get_task(conn, task_id)
            assert task is not None and task.status == 'todo'
