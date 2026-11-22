"""Actual synced registered tool -> supervisor producer -> live Board -> home courier."""
import json
from contextlib import contextmanager

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client, kanban_operator_call as courier, kanban_supervisor_requests as sr
from tests.hermes_cli import test_kanban_protect_stopped_board as support

composed = support.composed


def test_production_registered_synced_tool_through_start_push_and_home(composed, monkeypatch):
    from tools import kanban_tools
    c = composed
    actor_conn = kbc.connect(c['home']/'actor.db')
    rep = kbc.connect(c['home']/'replica.db')
    try:
        slug, ah, hub = c['board'], c['actor_hub'], c['hub']
        client.ensure_identity(actor_conn, slug, ah.ident.gateway)
        sync.set_mode(actor_conn, 'primary')
        client.pull(actor_conn, ah, slug, observers=False)
        monkeypatch.setattr(client, 'load_identity', lambda: ah.ident)
        monkeypatch.setattr(sync, '_LEASE_CLIENT', None)
        claimed = kb.claim_task(actor_conn, c['actor'], claimer='default')
        assert claimed is not None
        session = 'm2-real-producer'
        path = actor_conn.execute('PRAGMA database_list').fetchone()[2]
        for key, value in {'HERMES_KANBAN_DB':path, 'HERMES_KANBAN_TASK':c['actor'],
                           'HERMES_KANBAN_RUN_ID':str(claimed.current_run_id),
                           'HERMES_KANBAN_CLAIM_LOCK':claimed.claim_lock, 'HERMES_SESSION_ID':session}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
        with scoped_current_session_id(session):
            kb._append_event(actor_conn, c['actor'], 'owner_admitted', {'session_id':session}, run_id=claimed.current_run_id)
        client.push(actor_conn, ah, slug, ah.ident.gateway, primary=True)
        client.ensure_identity(rep, slug, hub.ident.gateway)
        client.pull(rep, hub, slug, observers=False)
        @contextmanager
        def replica(_):
            yield rep
        @contextmanager
        def tool_board(_):
            yield slug, actor_conn
        monkeypatch.setattr(courier, '_replica', replica)
        monkeypatch.setattr(courier, '_board_slug', lambda _: slug)
        monkeypatch.setattr(courier, '_evidence', lambda b, e: hub.row_evidence(b, 'task_events', e))
        monkeypatch.setattr(kanban_tools, '_board', tool_board)
        tool = next(item for item in kanban_tools._TOOLS if item[0] == 'kanban_protect_stopped')
        with scoped_current_session_id(session):
            result = json.loads(tool[2]({'task_id':c['target'], **c['args']}))
        assert result['ok'] and result['protection_state'] == 'requested', result
        with monkeypatch.context() as dispatch:
            dispatch.delenv('HERMES_KANBAN_TASK')
            sr.drain(actor_conn, limit=1, incoming=False)
        ev = actor_conn.execute("SELECT * FROM task_events WHERE kind='operator_call_requested'").fetchone()
        assert ev is not None and ev['run_id'] == claimed.current_run_id
        assert json.loads(ev['payload'])['version'] == 1
        assert client.push(actor_conn, ah, slug, ah.ident.gateway, primary=True).rejected == 0
        canonical = hub.row_evidence(slug, 'task_events', ev['uid'])['row']
        run_uid = actor_conn.execute('SELECT uid FROM task_runs WHERE id=?', (claimed.current_run_id,)).fetchone()[0]
        assert canonical['run_uid'] == run_uid
        client.pull(c['conn'], hub, slug, observers=False)
        client.pull(rep, hub, slug, observers=False)
        monkeypatch.setattr(client, 'load_identity', lambda: hub.ident)
        courier._MARK.clear()
        assert (json.loads(ev['payload'])['request_uid'], 'applied') in courier.apply_incoming(c['conn'])
        assert hub.row_evidence(slug, 'tasks', c['target'])['row']['status'] == 'triage'
    finally:
        actor_conn.close()
        rep.close()
