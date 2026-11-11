"""Omitted wire fields remain locally owned until their delivery is proven."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_run_rows as helpers

kanban_home = helpers.kanban_home
svc = helpers.svc
live = helpers.live


def omit_pin(live, svc):
    tid = kb.create_task(live, title="custody", assignee="worker")
    live.execute("UPDATE tasks SET model_pin_mandatory=1 WHERE id=?", (tid,))
    live.commit()
    hub = svc.hub("macbook")
    def ops(board, batch):
        results = []
        for op in batch:
            if op["table"] == "tasks" and "model_pin_mandatory" in op.get("fields", {}):
                results.append({"client_op_id": op["client_op_id"], "status": "rejected",
                                "reason": "forbidden_field", "detail": "not writable on tasks: model_pin_mandatory"})
            else:
                results.extend(hub.ops(board, [op])["results"])
        return {"results": results}
    for _ in range(3):
        client.push(live, SimpleNamespace(ops=ops), "default", "macbook", primary=True)
    return tid


@pytest.mark.parametrize("pending_wins", [False, True])
def test_authoritative_default_does_not_clobber_omitted_local_value(live, svc, pending_wins):
    tid = omit_pin(live, svc)
    version = live.execute("SELECT sync_version FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    change = sync.RemoteChange(seq=100, table="tasks", uid=tid, op="upsert",
                               fields={"model_pin_mandatory": None, "title": "remote title"},
                               version=version + 1, hlc="", origin_gateway="other")
    sync.apply_remote_changes(live, [change], new_cursor=100, board="default", local_pending_wins=pending_wins)
    row = live.execute("SELECT model_pin_mandatory,title FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row[0] == 1, "omitted field is still locally owned after successful stripped retry"
    assert row[1] == "remote title", "custody must be field-scoped, not hold the whole row"


def test_upgrade_queues_current_field_only_and_releases_after_ack(live, svc):
    tid = omit_pin(live, svc)
    version = live.execute("SELECT sync_version FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    change = sync.RemoteChange(seq=100, table="tasks", uid=tid, op="upsert",
                               fields={"model_pin_mandatory": None}, version=version + 1,
                               hlc="", origin_gateway="other")
    sync.apply_remote_changes(live, [change], new_cursor=100, board="default")
    restores = live.execute("SELECT * FROM kanban_sync_outbox WHERE op='restore'").fetchall()
    assert len(restores) == 1, "fresh authoritative field appearance should trigger one restoration"
    import json
    assert json.loads(restores[0]["fields"]) == {"model_pin_mandatory": 1}
    client.push(live, svc.hub("macbook"), "default", "macbook", primary=True)
    assert svc.board("default")["rows"]["tasks"][tid]["model_pin_mandatory"] == 1
    assert not live.execute("SELECT 1 FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()


@pytest.mark.parametrize("newer_edit", [False, True])
def test_restore_feed_echo_acknowledges_only_current_value(live, svc, newer_edit):
    import json
    tid = omit_pin(live, svc)
    version = live.execute("SELECT sync_version FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    change = sync.RemoteChange(100, "tasks", tid, "upsert", {"model_pin_mandatory": None}, version+1, "", "other")
    sync.apply_remote_changes(live, [change], new_cursor=100, board="default")
    op = live.execute("SELECT * FROM kanban_sync_outbox WHERE op='restore'").fetchone()
    if newer_edit:
        live.execute("UPDATE tasks SET model_pin_mandatory=0 WHERE id=?", (tid,))
        live.commit()
    echo = sync.RemoteChange(101, "tasks", tid, "upsert", json.loads(op['fields']), version+2, "", "macbook", op['op_id'])
    sync.apply_remote_changes(live, [echo], new_cursor=101, board="default")
    unresolved = live.execute("SELECT COUNT(*) FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()[0]
    assert bool(unresolved) is newer_edit
    assert live.execute("SELECT model_pin_mandatory FROM tasks WHERE id=?", (tid,)).fetchone()[0] == (0 if newer_edit else 1)
    if newer_edit:
        pending = live.execute("SELECT fields FROM kanban_sync_outbox WHERE op='restore' AND state='pending'").fetchall()
        assert [json.loads(r[0]) for r in pending] == [{'model_pin_mandatory': 0}]
        client.push(live, svc.hub('macbook'), 'default', 'macbook', primary=True)
        assert not live.execute("SELECT 1 FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()


def test_omitted_child_payload_survives_authoritative_row(live, svc):
    import json
    tid = kb.create_task(live, title="child custody", assignee="worker")
    uid, payload = live.execute("SELECT uid,payload FROM task_events WHERE task_id=? LIMIT 1", (tid,)).fetchone()
    hub = svc.hub("macbook")
    def ops(board, batch):
        results = []
        for op in batch:
            if op['table'] == 'task_events' and op['uid'] == uid and 'payload' in op.get('fields', {}):
                results.append({'client_op_id': op['client_op_id'], 'status': 'rejected',
                                'reason': 'forbidden_field', 'detail': 'not writable on task_events: payload'})
            else:
                results.extend(hub.ops(board, [op])['results'])
        return {'results': results}
    for _ in range(3):
        client.push(live, SimpleNamespace(ops=ops), 'default', 'macbook', primary=True)
    change = sync.RemoteChange(100, 'task_events', uid, 'upsert',
                               {'task_id': tid, 'payload': None, 'kind': 'remote_kind'}, 20, '', 'other')
    sync.apply_remote_changes(live, [change], new_cursor=100, board='default')
    assert live.execute('SELECT payload FROM task_events WHERE uid=?', (uid,)).fetchone()[0] == payload
    assert live.execute('SELECT kind FROM task_events WHERE uid=?', (uid,)).fetchone()[0] == 'remote_kind'
    restore = live.execute("SELECT fields FROM kanban_sync_outbox WHERE op='restore' AND row_uid=?", (uid,)).fetchone()
    # rows.ts rowCard needs task_id even when upsertChild can recover it from an existing row.
    assert json.loads(restore[0]) == {'payload': payload, 'task_id': tid}


def test_omission_does_not_hold_unrelated_divergence_repair(live, svc):
    import sqlite3
    tid = omit_pin(live, svc)
    shadow = sqlite3.connect(':memory:')
    shadow.row_factory = sqlite3.Row
    live.backup(shadow)
    try:
        shadow.execute('UPDATE kanban_sync_state SET applying=1')
        shadow.execute('UPDATE tasks SET model_pin_mandatory=NULL WHERE id=?', (tid,))
        shadow.commit()
        live.execute('UPDATE kanban_sync_state SET applying=1')
        live.execute("UPDATE tasks SET title='uncaptured' WHERE id=?", (tid,))
        live.execute('UPDATE kanban_sync_state SET applying=0')
        live.commit()
        assert client.repair_divergence(live, shadow, 'macbook') == {tid: ['title']}
    finally:
        shadow.close()


@pytest.mark.parametrize('merged', [False, True])
def test_ordinary_delivery_releases_custody_only_with_authoritative_proof(live, svc, merged):
    tid = omit_pin(live, svc)
    # A later ordinary captured edit can deliver the held field too.
    live.execute('UPDATE tasks SET model_pin_mandatory=0 WHERE id=?', (tid,))
    live.commit()
    hub = svc.hub('macbook')
    def ops(board, batch):
        response = hub.ops(board, batch)
        if merged:
            for result in response['results']:
                result.update(status='merged')
                result.pop('row', None)
        return response
    client.push(live, SimpleNamespace(ops=ops), 'default', 'macbook', primary=True)
    unresolved = live.execute("SELECT COUNT(*) FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()[0]
    assert bool(unresolved) is merged, 'merged without a row is not delivery proof'


def test_merged_restore_without_row_keeps_custody(live, svc):
    tid = omit_pin(live, svc)
    version = live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0]
    sync.apply_remote_changes(live, [sync.RemoteChange(100, 'tasks', tid, 'upsert',
        {'model_pin_mandatory': None}, version+1, '', 'other')], new_cursor=100, board='default')
    def ops(board, batch):
        return {'results': [{'client_op_id': op['client_op_id'], 'status': 'merged'} for op in batch]}
    client.push(live, SimpleNamespace(ops=ops), 'default', 'macbook', primary=True)
    assert live.execute("SELECT 1 FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()


def test_duplicate_original_verdict_cannot_grant_another_retry(live, svc):
    import json
    from hermes_cli.kanban_sync_compat import enqueue_retry
    tid = omit_pin(live, svc)
    receipt = live.execute('SELECT * FROM kanban_sync_omissions WHERE row_uid=? LIMIT 1', (tid,)).fetchone()
    original = dict(live.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (receipt['original_op_id'],)).fetchone())
    count = live.execute('SELECT COUNT(*) FROM kanban_sync_omissions').fetchone()[0]
    assert not enqueue_retry(live, original, {'fields': json.loads(original['fields']), 'hlc': original['hlc']},
        {'reason': 'forbidden_field', 'detail': 'not writable on tasks: model_pin_mandatory'})
    assert live.execute('SELECT COUNT(*) FROM kanban_sync_omissions').fetchone()[0] == count
    assert live.execute('SELECT compat_op_id FROM kanban_sync_omissions WHERE original_op_id=?',
                        (original['op_id'],)).fetchone()[0] == receipt['compat_op_id']


def test_successful_delete_without_wire_fields_retires_outbox(live, svc):
    tid = kb.create_task(live, title='delete control', assignee='worker')
    client.push(live, svc.hub('macbook'), 'default', 'macbook', primary=True)
    live.execute('DELETE FROM tasks WHERE id=?', (tid,))
    live.commit()
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='delete'").fetchone()
    client.push(live, svc.hub('macbook'), 'default', 'macbook', primary=True)
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='delete'").fetchone()


def test_applied_recalled_row_does_not_acknowledge_unsent_value(live, svc):
    tid = omit_pin(live, svc)
    live.execute('UPDATE tasks SET model_pin_mandatory=0 WHERE id=?', (tid,))
    live.commit()
    def recalled(board, batch):
        # Board board.ts stores and recalls its original Outcome, including row.
        return {'results': [{'client_op_id': op['client_op_id'], 'status': 'applied',
            'row': {'uid': op['uid'], 'model_pin_mandatory': 1}} for op in batch]}
    client.push(live, SimpleNamespace(ops=recalled), 'default', 'macbook', primary=True)
    assert live.execute("SELECT 1 FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()


def test_explicit_reconcile_retries_failed_restore_but_feed_does_not(live, svc, kanban_home):
    tid = omit_pin(live, svc)
    version = live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0]
    change = sync.RemoteChange(100, 'tasks', tid, 'upsert', {'model_pin_mandatory': None}, version+1, '', 'other')
    sync.apply_remote_changes(live, [change], new_cursor=100, board='default')
    def reject(board, batch):
        return {'results': [{'client_op_id': op['client_op_id'], 'status': 'rejected',
            'reason': 'forbidden_field', 'detail': 'not writable on tasks: model_pin_mandatory'} for op in batch]}
    client.push(live, SimpleNamespace(ops=reject), 'default', 'macbook', primary=True)
    failed = live.execute("SELECT restore_op_id FROM kanban_sync_omissions WHERE row_uid=?", (tid,)).fetchone()[0]
    for seq in (101, 102):
        sync.apply_remote_changes(live, [sync.RemoteChange(seq, 'tasks', tid, 'upsert',
            {'model_pin_mandatory': None}, version+seq, '', 'other')], new_cursor=seq, board='default')
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='restore' AND state='pending'").fetchone()
    # Fresh snapshot from the upgraded Board exposes the field, even though the old restore failed.
    svc.board('default')['rows']['tasks'][tid]['model_pin_mandatory'] = None
    client.reconcile('default', kanban_home / 'kanban.db', hub=svc.hub('macbook'))
    receipt = live.execute('SELECT * FROM kanban_sync_omissions WHERE row_uid=?', (tid,)).fetchone()
    assert receipt['state'] == 'delivered'
    assert receipt['restore_op_id'] != failed
    assert svc.board('default')['rows']['tasks'][tid]['model_pin_mandatory'] == 1


@pytest.mark.parametrize('failed_compat', [False, True])
def test_omitted_parent_does_not_hold_missing_chat_link_repair(live, svc, failed_compat):
    import sqlite3
    from tests.hermes_cli.test_kanban_sync_client import _gap_link
    tid = omit_pin(live, svc)
    shadow = sqlite3.connect(':memory:')
    shadow.row_factory = sqlite3.Row
    live.backup(shadow)
    try:
        uid = _gap_link(live, 'task_chat_links', tid, tid)
        if failed_compat:
            live.execute("INSERT INTO kanban_sync_outbox "
                "(op_id,table_name,row_uid,op,fields,hlc,created_at,state) "
                "VALUES ('failed-compat','tasks',?,'compat','{}','',1,'rejected')", (tid,))
            live.commit()
        client.repair_missing_children(live, shadow, 'macbook')
        pending = live.execute("SELECT row_uid FROM kanban_sync_outbox "
            "WHERE table_name='task_chat_links' AND state='pending'").fetchall()
        assert [r[0] for r in pending] == ([] if failed_compat else [uid])
    finally:
        shadow.close()


def test_delivered_receipt_with_retired_restore_stays_delivered(live, svc):
    from hermes_cli.kanban_sync_omissions import reconcile_page
    tid = omit_pin(live, svc)
    row = {'uid': tid, 'model_pin_mandatory': None}
    reconcile_page(live, 'tasks', [row])
    client.push(live, svc.hub('macbook'), 'default', 'macbook', primary=True)
    before = [tuple(r) for r in live.execute(
        'SELECT original_op_id,restore_op_id,state FROM kanban_sync_omissions ORDER BY original_op_id')]
    assert before and all(r[2] == 'delivered' for r in before)
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='restore'").fetchone()
    reconcile_page(live, 'tasks', [row])
    assert [tuple(r) for r in live.execute(
        'SELECT original_op_id,restore_op_id,state FROM kanban_sync_omissions ORDER BY original_op_id')] == before
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='restore'").fetchone()


def test_new_omission_does_not_reopen_older_delivered_receipt(live, svc):
    from hermes_cli.kanban_sync_omissions import reconcile_page
    tid = omit_pin(live, svc)
    row = {'uid': tid, 'model_pin_mandatory': None}
    reconcile_page(live, 'tasks', [row])
    client.push(live, svc.hub('macbook'), 'default', 'macbook', primary=True)
    before = [tuple(r) for r in live.execute(
        'SELECT original_op_id,restore_op_id,state FROM kanban_sync_omissions ORDER BY original_op_id')]
    assert before and all(r[2] == 'delivered' for r in before)
    live.execute("INSERT INTO kanban_sync_omissions "
        "(original_op_id,table_name,row_uid,field,compat_op_id,state) "
        "VALUES ('new-original','tasks',?,'model_pin_mandatory','new-compat','held')", (tid,))
    live.commit()
    reconcile_page(live, 'tasks', [row])
    old = [tuple(r) for r in live.execute(
        "SELECT original_op_id,restore_op_id,state FROM kanban_sync_omissions "
        "WHERE original_op_id!='new-original' ORDER BY original_op_id")]
    assert old == before
    receipt = live.execute("SELECT restore_op_id,state FROM kanban_sync_omissions "
        "WHERE original_op_id='new-original'").fetchone()
    assert receipt['state'] == 'restoring'
    restores = live.execute("SELECT op_id FROM kanban_sync_outbox WHERE op='restore'").fetchall()
    assert [r[0] for r in restores] == [receipt['restore_op_id']]


@pytest.mark.parametrize('condition', ['pending', 'missing_field', 'delivered'])
def test_reconcile_restore_controls(live, svc, condition):
    from hermes_cli.kanban_sync_omissions import reconcile_page
    tid = omit_pin(live, svc)
    row = {'uid': tid, 'model_pin_mandatory': None}
    reconcile_page(live, 'tasks', [row])
    before = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox WHERE op='restore'")]
    if condition == 'missing_field':
        row.pop('model_pin_mandatory')
        live.execute("UPDATE kanban_sync_outbox SET state='rejected' WHERE op='restore'")
    elif condition == 'delivered':
        live.execute("UPDATE kanban_sync_omissions SET state='delivered'")
    live.commit()
    identity = live.execute('SELECT restore_op_id FROM kanban_sync_omissions WHERE row_uid=?', (tid,)).fetchone()[0]
    reconcile_page(live, 'tasks', [row])
    assert live.execute('SELECT restore_op_id FROM kanban_sync_omissions WHERE row_uid=?', (tid,)).fetchone()[0] == identity
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE op='restore'").fetchone()[0] == len(before)


def test_restore_transport_loss_replays_cached_result_then_delivers_new_edit(live, svc):
    import copy
    tid = omit_pin(live, svc)
    version = live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0]
    sync.apply_remote_changes(live, [sync.RemoteChange(100, 'tasks', tid, 'upsert',
        {'model_pin_mandatory': None}, version+1, '', 'other')], new_cursor=100, board='default')
    hub = svc.hub('macbook')
    cached, writes = {}, []
    lose_reply = True
    def ops(board, batch):
        nonlocal lose_reply
        results = []
        for op in batch:
            identity = op['client_op_id']
            if identity not in cached:
                cached[identity] = copy.deepcopy(hub.ops(board, [op])['results'][0])
                writes.append(copy.deepcopy(op))
            results.append(copy.deepcopy(cached[identity]))
        if lose_reply:
            lose_reply = False
            raise OSError('reply lost after acceptance')
        return {'results': results}
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, SimpleNamespace(ops=ops), 'default', 'macbook', primary=True)
    restore = live.execute("SELECT op_id FROM kanban_sync_outbox WHERE op='restore'").fetchone()[0]
    assert svc.board('default')['rows']['tasks'][tid]['model_pin_mandatory'] == 1
    # An uncaptured newer value exercises the restore rearm, not an ordinary edit's delivery.
    live.execute('UPDATE kanban_sync_state SET applying=1')
    live.execute('UPDATE tasks SET model_pin_mandatory=0 WHERE id=?', (tid,))
    live.execute('UPDATE kanban_sync_state SET applying=0')
    live.commit()
    client.push(live, SimpleNamespace(ops=ops), 'default', 'macbook', primary=True)
    assert live.execute("SELECT 1 FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()
    assert len([op for op in writes if op['client_op_id'] == restore]) == 1
    client.push(live, SimpleNamespace(ops=ops), 'default', 'macbook', primary=True)
    assert svc.board('default')['rows']['tasks'][tid]['model_pin_mandatory'] == 0
    assert not live.execute("SELECT 1 FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()


def test_transient_restore_identity_change_still_releases_custody(live, svc):
    tid = omit_pin(live, svc)
    version = live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0]
    change = sync.RemoteChange(100, 'tasks', tid, 'upsert', {'model_pin_mandatory': None}, version+1, '', 'other')
    sync.apply_remote_changes(live, [change], new_cursor=100, board='default')
    def reject(board, batch):
        return {'results': [{'client_op_id': op['client_op_id'], 'status': 'rejected',
                             'reason': 'storage_error', 'detail': 'busy'} for op in batch]}
    client.push(live, SimpleNamespace(ops=reject), 'default', 'macbook', primary=True)
    live.execute("UPDATE kanban_sync_outbox SET retry_after=0 WHERE op='restore'")
    live.commit()
    client.push(live, svc.hub('macbook'), 'default', 'macbook', primary=True)
    assert not live.execute("SELECT 1 FROM kanban_sync_omissions WHERE state!='delivered'").fetchone()
