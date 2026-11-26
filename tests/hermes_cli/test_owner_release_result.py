"""Native historical completion, with a labelled canonical fixture adapter.

Real native custody and SQLite reopen; fixture rebinding is NOT composed DO proof.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_release_intent as intent
from tests.hermes_cli import test_owner_release_read as helpers

turn = helpers.turn
journal = helpers.journal


def application(body, wire):
    """Rebind an actual Board output fixture, retaining its original semantics."""
    from hermes_cli import kanban_release_certificate as cert
    value = json.loads((Path(__file__).parents[1] / 'fixtures/owner_release_application.json').read_text())
    old = value['consumption']['intent']
    pairs = [(old[k], body[k]) for k in ('board','task_id','home','episode','intent_uid')]
    pairs += list(zip(old['capability_episodes'], body['capability_episodes']))
    pairs += [(old['actor'][k], body['actor'][k]) for k in ('gateway','profile','session')]
    pairs += [(value['operation']['proposal']['delivery_uid'], wire['uid']), (intent.digest(old), intent.digest(body))]
    raw = cert.encode(value)
    for before, after in pairs:
        if before != after:
            raw = raw.replace(before, after)
    value = cert.parse(raw, max_bytes=8*1024*1024)
    comment_len = len(value['comments'][0]['body'])
    for event in [*value['events'], *value['operation']['proposal']['events']]:
        if event['kind'] == 'commented':
            payload = cert.parse(event['payload'])
            payload['len'] = comment_len
            event['payload'] = cert.encode(payload)
    return value


@pytest.mark.parametrize('field',['actor','delivery'])
def test_internally_valid_other_application_cannot_bind_local_request(completed,field):
    from hermes_cli import kanban_release_result as result, kanban_release_final as final
    c=completed
    body=json.loads(json.dumps(c.body))
    wire=json.loads(json.dumps(c.wire))
    if field=='actor':
        body['actor']['session']='other-qualified-session'
    else:
        wire['uid']='other-delivery'
    value=application(body,wire)
    assert final.validate(value,value['operation'])==value
    hub=Hub(c)
    hub.value=value
    with pytest.raises(ValueError,match='local identity'):
        result.reconcile(c.conn,hub,c.body)
    assert intent.stage(c.conn,c.body)=='delivery_pending'


def action():
    value = json.loads((Path(__file__).parents[1] / 'fixtures/owner_release_application.json').read_text())
    body = value['consumption']['intent']
    return {**{k: body[k] for k in intent._ACTION}, 'board':'default', 'task_id':'t_target', 'home':'foreign-home'}


@pytest.fixture
def completed(turn, journal):
    from hermes_cli import kanban_release_read as read, kanban_release_delivery as delivery
    body = intent.prepare(journal, action(), gate=helpers.gate())
    intent.advance(journal, body, expected='intent_journaled_unsent', stage='send_pending')
    intent.advance(journal, body, expected='send_pending', stage='authorized_pending_home')
    hub = helpers.Hub(body)
    first = read.prepare(journal, hub, body, seen=None)
    read.confirm(journal, hub, first.confirmation)
    nonce = first['release_read']['nonce']
    read.prepare(journal, hub, body, seen=nonce)
    wire = delivery.prepare(journal, body, nonce=nonce)
    value = application(body, wire)
    return SimpleNamespace(conn=journal, body=body, wire=wire, value=value)


class Hub:
    def __init__(self, c):
        self.ident = SimpleNamespace(gateway=c.body['actor']['gateway'])
        self.value = c.value
        self.calls = 0
    def owner_release_application(self, board, uid):
        self.calls += 1
        return self.value
    def row_evidence(self, *args, **kwargs):
        pytest.fail('completed application must precede mutable publication')


def test_native_send_records_completion_before_mutable_publication_and_reopens(completed):
    from hermes_cli import kanban_release_native as native, kanban_release_result as result, kanban_db_connect as kbc
    c = completed
    hub = Hub(c)
    before = tuple(c.conn.execute('SELECT count(*) FROM task_events').fetchone())
    got = native._send(c.conn, hub, c.body)
    assert got['release_stage'] == 'applied' and got['historical'] is True
    assert got['execution_clearance'] is False
    assert got['result'] == c.value['operation']['proposal']['application']['result']
    assert intent.stage(c.conn, c.body) == 'applied'
    assert result.saved(c.conn, c.body) == c.value
    assert tuple(c.conn.execute('SELECT count(*) FROM task_events').fetchone()) == before
    assert not c.conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_release_home_queue'").fetchone()
    def unavailable(*args):
        pytest.fail('immutable completed replay must not use HTTP')
    hub.owner_release_application = unavailable
    with kbc.connect_closing() as reopened:
        assert native._send(reopened, hub, c.body) == got
    assert hub.calls == 1


@pytest.mark.parametrize('damage', ['intent','delivery','comments','read_nonce','read_rows','read_ack','read_display','read_missing','transport'])
def test_exact_local_binding_required_before_result_write(completed, damage):
    from hermes_cli import kanban_release_result as result
    c = completed
    hub = Hub(c)
    actions = {
        'intent': lambda: c.value['consumption']['intent'].update(note='forged'),
        'delivery': lambda: c.value['operation']['proposal'].update(delivery_uid='other-delivery'),
        'comments': lambda: c.value['consumption']['consume'].update(seen_comments=[{}]),
        'transport': lambda: setattr(hub.ident, 'gateway', 'foreign'),
        'read_nonce': lambda: c.conn.execute("UPDATE native_owner_release_reads SET nonce=?", ('a'*64,)),
        'read_rows': lambda: c.conn.execute("UPDATE native_owner_release_reads SET rows_bytes='{}'"),
        'read_ack': lambda: c.conn.execute('UPDATE native_owner_release_reads SET acknowledged=0'),
        'read_display': lambda: c.conn.execute('UPDATE native_owner_release_reads SET displayed_invocation=NULL'),
        'read_missing': lambda: c.conn.execute('DELETE FROM native_owner_release_reads'),
    }
    actions[damage]()
    c.conn.commit()
    with pytest.raises(ValueError):
        result.reconcile(c.conn, hub, c.body)
    assert intent.stage(c.conn, c.body) == 'delivery_pending'


@pytest.mark.parametrize('status',[401,403,409,500])
def test_only_actual_private_404_is_absence(completed,status):
    from hermes_cli import kanban_release_result as result
    from hermes_cli.kanban_sync_client import HubError
    c=completed
    hub=Hub(c)
    def failed(*args):
        raise HubError(status, 'fixture')
    hub.owner_release_application=failed
    with pytest.raises(HubError):
        result.reconcile(c.conn,hub,c.body)
    assert intent.stage(c.conn,c.body)=='delivery_pending'


@pytest.mark.parametrize('damage',['deleted_receipt','missing_anchor','wrong_hash','stage','nonce','noncanonical'])
def test_completed_replay_never_reconstructs_missing_or_corrupt_custody(completed,damage):
    from hermes_cli import kanban_release_result as result
    c=completed
    hub=Hub(c)
    result.reconcile(c.conn,hub,c.body)
    statements={
        'deleted_receipt':(f'DROP TRIGGER {result.TABLE}_delete',f'DELETE FROM {result.TABLE}'),
        'missing_anchor':('DROP TRIGGER native_release_result_completed','DROP TRIGGER native_release_result_anchor',
                          'UPDATE native_owner_release_intents SET result_digest=NULL'),
        'wrong_hash':(f'DROP TRIGGER {result.TABLE}_update','DROP TRIGGER native_release_result_completed',
                      'DROP TRIGGER native_release_result_anchor',f"UPDATE {result.TABLE} SET digest='bad'",
                      "UPDATE native_owner_release_intents SET result_digest='bad'"),
        'stage':('DROP TRIGGER native_release_result_completed',"UPDATE native_owner_release_intents SET stage='consumed'"),
        'nonce':("UPDATE native_owner_release_reads SET nonce='changed'",),
        'noncanonical':(f'DROP TRIGGER {result.TABLE}_update',f"UPDATE {result.TABLE} SET certificate=' '||certificate"),
    }
    for statement in statements[damage]:
        c.conn.execute(statement)
    c.conn.commit()
    with pytest.raises(ValueError):
        result.reconcile(c.conn,hub,c.body)
    assert hub.calls==1


def test_coherent_canonical_comments_must_equal_original_local_read(completed):
    from hermes_cli import kanban_release_certificate as cert, kanban_release_result as result, kanban_release_final as final
    c=completed
    comment={'uid':'other-comment','task_id':c.body['task_id'],'author':'owner','body':'other read',
             'created_at':1,'seq':1,'version':1,'updated_at':1,'hlc':'1','origin_gateway':'foreign-home','deleted_at':None}
    value=cert.parse(cert.encode(c.value).replace('"seen_comments":[]','"seen_comments":'+cert.encode([comment])),max_bytes=final.MAX_BYTES)
    assert final.validate(value,value['operation'])==value
    hub=Hub(c)
    hub.value=value
    with pytest.raises(ValueError,match='local identity'):
        result.reconcile(c.conn,hub,c.body)


def test_origin_receipt_insert_requires_its_own_writer(completed):
    from hermes_cli import kanban_release_result as result
    c=completed
    result.install(c.conn)
    with pytest.raises(sqlite3.DatabaseError,match='immutable'):
        with kb.write_txn(c.conn):
            c.conn.execute(f'INSERT INTO {result.TABLE} VALUES (?,?,?)',('other','{}','bad'))


def test_private_404_leaves_no_completion(completed):
    from hermes_cli import kanban_release_result as result
    from hermes_cli.kanban_sync_client import HubError
    c=completed
    hub=Hub(c)
    def absent(*args):
        raise HubError(404,'not applied')
    hub.owner_release_application=absent
    assert result.reconcile(c.conn,hub,c.body) is None
    assert intent.stage(c.conn,c.body)=='delivery_pending'


def test_revoked_during_private_get_writes_nothing(completed,turn):
    from hermes_cli import kanban_release_result as result
    c=completed
    hub=Hub(c)
    def revoke(*args):
        turn.release()
        return c.value
    hub.owner_release_application=revoke
    with pytest.raises(ValueError,match='custody'):
        result.reconcile(c.conn,hub,c.body)
    assert c.conn.execute('SELECT stage FROM native_owner_release_intents').fetchone()[0]=='delivery_pending'


def test_receipt_marker_and_terminal_stage_roll_back_together(completed):
    from hermes_cli import kanban_release_result as result
    c=completed
    result.install(c.conn)
    c.conn.execute("CREATE TRIGGER fail_terminal BEFORE UPDATE OF stage ON native_owner_release_intents WHEN NEW.stage='applied' BEGIN SELECT RAISE(ABORT,'fixture terminal failure'); END")
    with pytest.raises(sqlite3.DatabaseError,match='fixture terminal failure'):
        result.reconcile(c.conn,Hub(c),c.body)
    assert not c.conn.execute(f'SELECT 1 FROM {result.TABLE}').fetchone()
    assert c.conn.execute('SELECT result_digest,stage FROM native_owner_release_intents').fetchone()[:]==(None,'delivery_pending')


@pytest.mark.parametrize('mode',['update','delete','replace','rowid','anchor','journal_delete','journal_replace','journal_update'])
def test_completed_custody_is_immutable(completed,mode):
    from hermes_cli import kanban_release_result as result
    c=completed
    result.reconcile(c.conn,Hub(c),c.body)
    row=c.conn.execute(f'SELECT rowid,* FROM {result.TABLE}').fetchone()
    sql,args={
        'update':(f"UPDATE {result.TABLE} SET digest='bad'",()),
        'delete':(f'DELETE FROM {result.TABLE}',()),
        'replace':(f'INSERT OR REPLACE INTO {result.TABLE} SELECT * FROM {result.TABLE}',()),
        'rowid':(f'INSERT OR REPLACE INTO {result.TABLE}(rowid,intent_uid,certificate,digest) VALUES (?,?,?,?)',(row[0],'other',row[2],row[3])),
        'anchor':('UPDATE native_owner_release_intents SET result_digest=NULL',()),
        'journal_update':("UPDATE native_owner_release_intents SET digest='changed'",()),
        'journal_delete':('DELETE FROM native_owner_release_intents',()),
        'journal_replace':('INSERT OR REPLACE INTO native_owner_release_intents SELECT * FROM native_owner_release_intents',()),
    }[mode]
    with pytest.raises(sqlite3.DatabaseError,match='immutable'):
        with kb.write_txn(c.conn):
            c.conn.execute(sql,args)
    assert result.saved(c.conn,c.body)==c.value


def test_applied_without_result_never_reports_success(completed):
    from hermes_cli import kanban_release_native as native, kanban_release_result as result
    c=completed
    c.conn.execute("UPDATE native_owner_release_intents SET stage='applied'")
    c.conn.commit()
    with pytest.raises(ValueError,match='result missing'):
        result.saved(c.conn,c.body)
    with pytest.raises(ValueError,match='result'):
        native._send(c.conn,Hub(c),c.body)


def test_explicit_rowid_replacement_refuses_even_inside_result_permit(completed):
    from hermes_cli import kanban_release_result as result
    c=completed
    result.reconcile(c.conn,Hub(c),c.body)
    row=c.conn.execute(f'SELECT rowid,certificate,digest FROM {result.TABLE}').fetchone()
    with pytest.raises(sqlite3.DatabaseError,match='immutable'):
        with kb.write_txn(c.conn):
            token=result._PERMIT.set(c.conn)
            try:
                c.conn.execute(f'INSERT OR REPLACE INTO {result.TABLE}(rowid,intent_uid,certificate,digest) VALUES (?,?,?,?)',
                               (row[0],'other-intent',row[1],row[2]))
            finally:
                result._PERMIT.reset(token)
    assert result.saved(c.conn,c.body)==c.value


def test_new_intent_and_pending_neighbor_progress_after_completed_connection_reopen(completed):
    from hermes_cli import kanban_release_result as result, kanban_db_connect as kbc
    c=completed
    pending=intent.prepare(c.conn,{**action(),'episode':'pending-neighbor'},gate=helpers.gate())
    result.reconcile(c.conn,Hub(c),c.body)
    with kbc.connect_closing() as reopened:
        fresh=intent.prepare(reopened,{**action(),'episode':'successor-scope'},gate=helpers.gate())
        assert intent.advance(reopened,fresh,expected='intent_journaled_unsent',stage='send_pending')
        assert intent.advance(reopened,pending,expected='intent_journaled_unsent',stage='send_pending')
        assert intent.stage(reopened,c.body)=='applied'
        assert result.saved(reopened,c.body)==c.value
