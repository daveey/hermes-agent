"""Actual registered tool, home courier and requester settlement after lost ACK."""
import json
from contextlib import contextmanager

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client, kanban_operator_call as courier, kanban_supervisor_requests as sr
from tests.hermes_cli import test_kanban_protect_stopped_board as support

composed = support.composed


# The name starts with test_production_ so the PR fixture yields its production shape.
@pytest.mark.parametrize('renewed', [False, True])
def test_production_requester_terminal(composed, monkeypatch, renewed):
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
        assert client.push(actor_conn, ah, slug, ah.ident.gateway, primary=True).rejected == 0
        client.pull(c['conn'], hub, slug, observers=False)
        client.pull(rep, hub, slug, observers=False)
        monkeypatch.setattr(client, 'load_identity', lambda: hub.ident)
        courier._MARK.clear()
        from hermes_cli import kanban_protect_transport as transport, kanban_protect_operator as _operator
        uid = json.loads(ev['payload'])['request_uid']
        original = client.Hub.ops
        def lost(self, b, ops):
            answer = original(self, b, ops)
            if any(op.get('op') == 'protect_stopped' for op in ops):
                assert answer['results'][0]['status'] == 'applied'
                raise TimeoutError('lost reply')
            return answer
        monkeypatch.setattr(client.Hub, 'ops', lost)
        assert (uid, 'accepted_pending_canonical') in courier.apply_incoming(c['conn'])
        monkeypatch.setattr(client.Hub, 'ops', original)
        if renewed:
            c['wire'] = json.loads(transport.command(c['conn'], uid)['request_json'])
            support.renew_adoption(c)
        courier._MARK.clear()
        _operator._BACKOFF.clear()
        home_done = courier.apply_incoming(c['conn'])
        print('PROBE2 renewed', renewed, 'home outcome', home_done)
        print('PROBE2 home execute log', [tuple(r) for r in c['conn'].execute("SELECT outcome, detail FROM operator_calls WHERE side='execute'")])
        assert client.push(c['conn'], hub, slug, hub.ident.gateway, primary=True).rejected == 0
        client.pull(rep, hub, slug, observers=False)
        monkeypatch.setattr(client, 'load_identity', lambda: ah.ident)
        client.pull(actor_conn, ah, slug, observers=False)
        with monkeypatch.context() as dispatch:
            dispatch.delenv('HERMES_KANBAN_TASK')
            out = sr.drain(actor_conn, limit=1, incoming=False)
        print('PROBE2 requester drain', out['processed'])
        print('PROBE2 requester supervisor_requests', [tuple(r) for r in actor_conn.execute('SELECT status, error, result_json FROM supervisor_requests')])
        print('PROBE2 requester request log', [tuple(r) for r in actor_conn.execute("SELECT outcome, detail FROM operator_calls WHERE side='request'")])
        rows = actor_conn.execute('SELECT status, error, result_json FROM supervisor_requests').fetchall()
        assert len(rows) == 1
        assert rows[0]['status'] == 'done' and rows[0]['error'] is None
        result = json.loads(rows[0]['result_json'])[0]
        assert result['outcome'] == ('recorded' if renewed else 'applied')
        assert result['protection']['protection_state'] == ('recorded_hold_unconfirmed' if renewed else 'held_confirmed')
        assert result['protection']['execution_authority'] is False
        assert result['protection']['dependency_satisfied'] is False
        assert result['detail'] == 'Administrative protection only; no execution authority'
        assert actor_conn.execute("SELECT outcome FROM operator_calls WHERE side='request'").fetchone()[0] == 'done'
    finally:
        actor_conn.close()
        rep.close()
