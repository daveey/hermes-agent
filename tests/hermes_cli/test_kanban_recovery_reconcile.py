"""Recovery authority survives row-loss restoration, not insertion-order replay."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_resume_exec as rx
from hermes_cli import kanban_resume_requester as rr
from hermes_cli import kanban_supervisor_requests as sr
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import test_kanban_worker_node as worker_node
from tests.hermes_cli.test_kanban_recovery_authority import _foreign_park
from hermes_cli import kanban_continuation as kc
from tests.hermes_cli.test_kanban_sync_reconcile import _without_capture

gws = worker_node.gws
svc = worker_node.svc


def _exhaust(home, target):
    assert kb.claim_task(home.conn, target) is not None
    home.round()  # receive service-authored claimed before the park
    run = kb.get_task(home.conn, target).current_run_id
    out = kc.continue_task(home.conn, target, checkpoint='progress', expected_run_id=run, max_continuations=0)
    assert out['reason_code'] == 'continuation_exhausted'
    assert kb.block_task(home.conn, target, kind='transient', expected_run_id=run,
                         reason='continuation_exhausted: split the work or reset the continuation budget')


def _restore(home, svc, event_id):
    home.round()
    uid = home.conn.execute('SELECT uid FROM task_events WHERE id=?', (event_id,)).fetchone()[0]
    _without_capture(home.conn, 'DELETE FROM task_events WHERE id=?', (event_id,))
    ksc.reconcile(worker_node.BOARD, home.home / 'kanban.db', hub=svc.hub(home.name))
    restored = home.conn.execute('SELECT id FROM task_events WHERE uid=?', (uid,)).fetchone()[0]
    assert restored > event_id
    return restored


def test_restored_current_park_keeps_requester_and_executes_once(gws, svc):
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    [rec] = rr.ensure_recovery_cards(home.conn)
    assert rx.refusal(home.conn, rec, target) is None
    _restore(home, svc, rx._park_event_id(home.conn, target))
    assert rx.refusal(home.conn, rec, target) is None
    assert rr.open_recovery_card(home.conn, target) == rec
    assert rr.ensure_recovery_cards(home.conn) == []
    home.tick()
    run = kb.get_task(home.conn, rec).current_run_id
    assert run is not None
    out = sr.submit(home.conn, env_task=rec, env_run=str(run),
                    ops=[{'op': 'resume', 'task_id': target}], reason='progress')
    assert sr.drain(home.conn)['processed'] == [(out['request_id'], 'done')]
    assert sr.drain(home.conn)['processed'] == []
    assert len(rx._resume_events(home.conn, target)) == 1
    home.round()
    events = svc.board(worker_node.BOARD)['rows']['task_events'].values()
    assert sum(e['task_id'] == target and e['kind'] == 'unblocked' for e in events) == 1


def test_restored_stale_creation_does_not_gain_authority(gws, svc):
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    [old] = rr.ensure_recovery_cards(home.conn)
    assert kb.unblock_task(home.conn, target)
    _exhaust(home, target)
    assert 'predates' in rx.refusal(home.conn, old, target)
    _restore(home, svc, rx._created_event_id(home.conn, old))
    assert 'predates' in rx.refusal(home.conn, old, target)


def test_restored_historical_park_does_not_replace_current(gws, svc):
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    [old] = rr.ensure_recovery_cards(home.conn)
    first_park = rx._park_event_id(home.conn, target)
    assert kb.unblock_task(home.conn, target)
    _exhaust(home, target)
    [new] = rr.ensure_recovery_cards(home.conn)
    current = rx._park_event_id(home.conn, target)
    _restore(home, svc, first_park)
    assert rx._park_event_id(home.conn, target) == current
    assert rx.refusal(home.conn, new, target) is None
    assert 'predates' in rx.refusal(home.conn, old, target)
    assert rr.open_recovery_card(home.conn, target) == new
    assert rr.ensure_recovery_cards(home.conn) == []


def test_restored_receipt_and_equal_timestamps_keep_causal_order(gws, svc):
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    [rec] = rr.ensure_recovery_cards(home.conn)
    # Deliberately remove timestamp discrimination; these are event payloads, not
    # a mocked clock or mocked recovery verdict.
    with kb.write_txn(home.conn):
        home.conn.execute('UPDATE task_events SET created_at=(SELECT MAX(created_at) FROM task_events)')
    receipt = home.conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='continuation_exhausted'",
                                (target,)).fetchone()[0]
    _restore(home, svc, receipt)
    assert rx.refusal(home.conn, rec, target) is None
    unrelated = kb.create_task(home.conn, title='unrelated', assignee='default')
    assert 'creator chain' in rx.refusal(home.conn, unrelated, target)
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE tasks SET block_kind='needs_input', human_request=? WHERE id=?",
                          ('{"action":"decide"}', target))
    assert 'human_gate' in rx.refusal(home.conn, rec, target)
    assert rr.ensure_recovery_cards(home.conn) == []


def test_legacy_synced_park_without_provenance_fails_closed(gws):
    from hermes_cli.kanban_recovery_history import META
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    park = rx._park_event_id(home.conn, target)
    with kb.write_txn(home.conn):
        home.conn.execute('UPDATE task_events SET payload=json_remove(payload, ?) WHERE id=?',
                          ('$.' + META, park))
    assert rx.exhaustion_refusal(home.conn, target) is not None
    assert rr.ensure_recovery_cards(home.conn) == []


def test_legacy_synced_history_keeps_triage_hold_but_not_authority(gws):
    from hermes_cli.kanban_recovery_history import META
    home, worker = gws
    target = _foreign_park(home, worker)
    home.enter()
    [rec] = rr.ensure_recovery_cards(home.conn)
    # Pre-upgrade history: no lifecycle event carries causal evidence; the link has only the uid.
    with kb.write_txn(home.conn):
        home.conn.execute('UPDATE task_events SET payload=json_remove(payload, ?, ?) WHERE task_id IN (?, ?)',
                          ('$.' + META, '$.park_identity', target, rec))
    assert rr.open_recovery_card(home.conn, target) == rec  # stale-triage sweep still leaves it alone
    assert rx.exhaustion_refusal(home.conn, target) is not None
    assert rx.refusal(home.conn, rec, target) is not None


def test_goal_park_and_receipt_restore_without_changing_judge_evidence(gws, svc):
    from hermes_cli import kanban_goal_recovery as goal
    home, _ = gws
    home.enter()
    target = kb.create_task(home.conn, title='judged goal', assignee='default', goal_mode=True)
    assert kb.claim_task(home.conn, target)
    home.round()
    task = kb.get_task(home.conn, target)
    assert goal.park(home.conn, task, 'missing prerequisite', expected_run_id=task.current_run_id)
    rec = kb.create_task(home.conn, title='repair prerequisite', assignee='default', creator_task_id=target)
    assert rx.refusal(home.conn, rec, target) is None
    _restore(home, svc, rx._park_event_id(home.conn, target))
    receipt = home.conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='goal_unachievable'",
                                (target,)).fetchone()[0]
    _restore(home, svc, receipt)
    assert goal.current_park(home.conn, target)
    assert rx.refusal(home.conn, rec, target) is None
