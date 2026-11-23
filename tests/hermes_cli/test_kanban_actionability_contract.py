"""Episode/deadline boundaries from bounded slice review; synthetic diagnostic fixtures. [daveey]"""
from __future__ import annotations

import sqlite3
import json

from hermes_cli.kanban_actionability import project
from hermes_cli.kanban_actionability_sources import describe


def fixture():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('CREATE TABLE tasks(id TEXT,status TEXT,assignee TEXT,created_at INTEGER);'
                       'CREATE TABLE task_events(id INTEGER PRIMARY KEY,uid TEXT,task_id TEXT,kind TEXT,payload TEXT,created_at INTEGER);'
                       'CREATE TABLE task_links(parent_id TEXT,child_id TEXT);'
                       "INSERT INTO tasks VALUES('root','triage','default',100);"
                       "INSERT INTO tasks VALUES('owner','ready','default',100);")
    return conn


def event(conn, uid, kind, data, at):
    conn.execute('INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES(?,\'root\',?,?,?)',
                 (uid, kind, json.dumps(data), at))


def test_containment_precedes_ordinary_spend_but_not_protected_stop():
    facts = {'task': {'id': 'root', 'status': 'triage', 'assignee': 'default', 'created_at': 100},
             'containment': 'contained-a', 'stop': {'cause': 'spend_budget'}}
    row = project(facts, now=200)
    assert row['action'] == 'resolve_release_authority'
    facts['stop']['cause'] = 'scope_stopped'
    assert project(facts, now=200)['action'] == 'decide_scope'


def test_dependency_display_words_do_not_change_material_identity():
    facts = {'task': {'id': 'root', 'status': 'todo', 'assignee': 'default', 'created_at': 100},
             'dependencies': [{'task_id': 'parent', 'status': 'blocked',
                               'owner': {'kind': 'assignee', 'refs': ['default']}, 'trigger': 'first wording'}]}
    before = project(facts, now=200)
    facts['dependencies'][0]['trigger'] = 'reworded display only'
    assert project(facts, now=200)['contract_key'] == before['contract_key']


def test_receipt_only_archived_parent_is_unknown_not_satisfied():
    conn = fixture()
    conn.execute("UPDATE tasks SET status='archived' WHERE id='root'")
    conn.execute("UPDATE tasks SET status='todo' WHERE id='owner'")
    conn.execute("INSERT INTO task_links VALUES('root','owner')")
    event(conn, 'receipt', 'decision_retained', {'episode_uid': 'stop',
          'disposition': {'reason': 'scope_stopped'}}, 101)
    row = describe(conn, 'owner', now=200)
    assert row['dependencies'][0]['task_id'] == 'root'
    assert row['evidence_state'] == 'unknown'
    assert row['action'] == 'satisfy_dependency'
    conn.close()


def test_retained_actual_prerequisite_instruction_and_release_condition_are_visible():
    conn = fixture()
    event(conn, 'stop', 'spend_budget_triaged', {'cause': 'capability_unavailable'}, 100)
    event(conn, 'receipt', 'decision_retained', {'episode_uid': 'stop', 'next_action': {
        'owner': {'kind': 'task', 'refs': ['owner']}, 'instruction': 'Restore native pickup, then verify the claimed process',
        'blocker': {'prerequisite': 'native kanban.sync.pickup on worker-x', 'trigger': 'Verified native pickup',
                    'evidence': ['registry-snapshot']},
        'release': {'authority': 'existing_owner', 'condition': 'Native process claimed and verified'},
        'escalation': {'at': 500}}}, 101)
    row = describe(conn, 'root', now=200)
    assert row['blocker']['prerequisite'] == 'native kanban.sync.pickup on worker-x'
    assert row['blocker']['trigger'] == 'Verified native pickup'
    assert row['instruction'] == 'Restore native pickup, then verify the claimed process'
    assert row['release']['condition'] == 'Native process claimed and verified'
    assert row['release']['authority'] == 'unavailable'
    conn.close()


def test_diagnostic_notice_cannot_renew_retained_deadline():
    conn = fixture()
    event(conn, 'stop-a', 'spend_budget_triaged', {'cause': 'scope_stopped'}, 100)
    event(conn, 'retained-a', 'decision_retained', {'episode_uid': 'stop-a',
          'next_action': {'owner': {'kind': 'task', 'refs': ['owner']}, 'escalation': {'at': 500}}}, 101)
    before = describe(conn, 'root', now=400)
    event(conn, 'notice-a', 'spend_budget_decision_ready', {'episode': 'stop-a',
          'producer': {'kind': 'supervisor', 'task_id': 'owner'}}, 102)
    after = describe(conn, 'root', now=400)
    assert before['contract_key'] == after['contract_key']
    assert before['escalation']['at'] == after['escalation']['at'] == 500
    conn.close()


def test_unavailable_route_does_not_fall_back_to_workload_assignee():
    conn = fixture()
    event(conn, 'stop-a', 'spend_budget_triaged', {'cause': 'scope_stopped'}, 100)
    event(conn, 'retained-a', 'decision_retained', {'episode_uid': 'stop-a',
          'next_action': {'owner': {'kind': 'unavailable', 'refs': []}, 'escalation': {'at': 500}}}, 101)
    row = describe(conn, 'root', now=400)
    assert row['owner']['kind'] == 'unavailable'
    assert row['escalation']['at'] == 500
    assert 'owner_missing' in row['violations']
    conn.close()


def test_new_stop_cannot_reuse_prior_episode_owner():
    conn = fixture()
    event(conn, 'stop-a', 'spend_budget_triaged', {}, 100)
    event(conn, 'notice-a', 'spend_budget_decision_ready', {'episode': 'stop-a',
          'producer': {'kind': 'supervisor', 'task_id': 'owner'}}, 101)
    event(conn, 'promoted', 'promoted_manual', {}, 102)
    event(conn, 'stop-b', 'spend_budget_triaged', {}, 103)
    row = describe(conn, 'root', now=104)
    assert row['owner']['kind'] == 'unavailable'
    assert 'owner_missing' in row['violations']
    conn.close()


def test_stale_future_schedule_cannot_extend_scope_stop_deadline():
    facts = {'task': {'id': 'root', 'status': 'scheduled', 'assignee': 'default', 'created_at': 100},
             'source_at': 100, 'stop': {'cause': 'scope_stopped'}, 'stop_episode': 'stop-a',
             'wake_at': 1000000, 'awaiting': {'kind': 'clock'}}
    row = project(facts, now=90000)
    assert row['escalation']['at'] is None
    assert 'escalation_deadline_missing' in row['violations']
    assert 'escalation_overdue' not in row['violations']
    assert row['release']['authority'] == 'unavailable'


def test_delivery_state_is_not_material_contract_identity():
    facts = {'task': {'id': 'root', 'status': 'running', 'assignee': 'default', 'created_at': 100},
             'pending_command': {'request_uid': 'request-a', 'request_sha256': 'a' * 64,
                                 'delivery_state': 'pending'}}
    before = project(facts, now=200)
    facts['pending_command']['delivery_state'] = 'unknown'
    after = project(facts, now=300)
    assert before['contract_key'] == after['contract_key']
    assert before['escalation']['at'] == after['escalation']['at']


def test_missing_restriction_page_preserves_hold_only_receipt_diagnostics():
    conn = fixture()
    conn.execute("UPDATE tasks SET status='ready' WHERE id='root'")
    event(conn, 'receipt', 'decision_retained', {'episode_uid': 'not-yet-visible',
          'disposition': {'reason': 'scope_stopped'}}, 101)
    row = describe(conn, 'root', now=200)
    assert row['evidence_state'] == 'unknown'
    assert row['action'] == 'decide_scope'
    assert row['release']['authority'] == 'unavailable'
    conn.close()


def test_explicit_promotion_can_supersede_ordinary_receipt_but_not_scope_stop():
    for reason in ('capability_unavailable', 'scope_stopped'):
        conn = fixture()
        conn.execute("UPDATE tasks SET status='ready' WHERE id='root'")
        event(conn, 'receipt', 'decision_retained', {'episode_uid': 'old', 'disposition': {'reason': reason}}, 101)
        event(conn, 'promotion', 'promoted_manual', {}, 102)
        row = describe(conn, 'root', now=200)
        assert row['action'] == ('decide_scope' if reason == 'scope_stopped' else 'execute_current_scope')
        conn.close()


def test_synced_uidless_events_never_use_replica_local_integer_identity():
    conn = fixture()
    conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
    event(conn, None, 'spend_budget_triaged', {}, 100)
    row = describe(conn, 'root', now=200)
    assert row['evidence_state'] == 'unknown'
    assert row['source_refs'] == []
    conn.close()


def test_protected_archived_dependency_remains_visible():
    conn = fixture()
    conn.execute("UPDATE tasks SET status='todo' WHERE id='owner'")
    conn.execute("UPDATE tasks SET status='archived' WHERE id='root'")
    conn.execute("INSERT INTO task_links VALUES('root','owner')")
    event(conn, 'stop', 'spend_budget_triaged', {'cause': 'safety_refusal'}, 100)
    row = describe(conn, 'owner', now=200)
    assert row['action'] == 'satisfy_dependency'
    assert row['dependencies'][0]['task_id'] == 'root'
    conn.close()


def test_report_denies_sql_writes_and_network(monkeypatch):
    from hermes_cli.kanban_actionability_cli import report
    import socket
    conn = fixture()
    def deny_network(*args, **kwargs):
        raise AssertionError('read-only report attempted network')
    monkeypatch.setattr(socket, 'create_connection', deny_network)
    writes = []
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_ALTER_TABLE}
    def authorize(action, *args):
        if action in denied:
            writes.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    result = report(conn, now=200)
    assert result['total_nonterminal'] == 2
    assert not writes
    conn.close()


def test_uidless_stop_does_not_take_notice_timestamp_as_episode_root():
    conn = fixture()
    conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
    event(conn, None, 'spend_budget_triaged', {}, 100)
    event(conn, None, 'spend_budget_decision_ready', {'producer': {'kind': 'unavailable'}}, 10000)
    row = describe(conn, 'root', now=20000)
    assert row['first_observed'] == 100
    assert row['escalation']['at'] is None
    assert 'escalation_deadline_missing' in row['violations']
    assert row['evidence_state'] == 'unknown'
    conn.close()


def test_partial_optional_schemas_and_plain_tuple_connection_are_readable():
    from hermes_cli.kanban_actionability_cli import report
    conn = fixture()
    conn.row_factory = None
    conn.executescript('CREATE TABLE kanban_sync_state(board_id TEXT);'
                       'CREATE TABLE task_wake_checks(task_id TEXT);'
                       'CREATE TABLE task_runs(task_id TEXT);')
    conn.execute("UPDATE tasks SET status='running' WHERE id='root'")
    before = list(conn.iterdump())
    result = report(conn, now=200)
    row = next(r for r in result['rows'] if r['task_id'] == 'root')
    assert row['evidence_state'] == 'unknown'
    assert 'running_without_current_run' not in row['violations']
    assert list(conn.iterdump()) == before
    conn.close()


def test_explicit_deadline_never_depends_on_source_age_and_terminal_is_na():
    facts = {'task': {'id': 'x', 'status': 'blocked', 'created_at': 1}, 'source_at': 2}
    row = project(facts, now=100000)
    assert row['escalation']['at'] is None
    assert row['escalation']['state'] == 'unavailable'
    facts['deadline_at'] = 123
    assert project(facts, now=100000)['escalation']['at'] == 123
    assert 'escalation_overdue' in project(facts, now=100000)['violations']
    facts['task']['status'] = 'done'
    terminal = project(facts, now=100000)
    assert terminal['action'] == 'no_open_action'
    assert terminal['violations'] == [] and terminal['escalation']['at'] is None


def test_null_uid_on_unsynced_history_keeps_local_source_identity():
    conn = fixture()
    event(conn, None, 'spend_budget_triaged', {}, 100)
    assert describe(conn, 'root', now=200)['source_refs'] == ['1']
    conn.close()


def test_malformed_human_request_is_unknown_repair_not_human_ask():
    conn = fixture()
    conn.execute('ALTER TABLE tasks ADD COLUMN human_request TEXT')
    conn.execute("UPDATE tasks SET status='blocked',human_request=? WHERE id='root'",
                 (json.dumps({'action': {'bad': 'not text'}}),))
    row = describe(conn, 'root', now=200)
    assert row['evidence_state'] == 'unknown'
    assert row['action'] == 'repair_lifecycle_contract'
    conn.close()


def test_pending_multiple_commands_have_stable_identity_and_unknown_custody():
    conn = fixture()
    conn.executescript('CREATE TABLE kanban_failure_commands(task_id TEXT,run_uid TEXT,request_uid TEXT,'
                       'operation_kind TEXT,delivery_state TEXT,canonical_receipt_json TEXT);')
    for uid in ('b', 'a'):
        conn.execute("INSERT INTO kanban_failure_commands VALUES('root',?,?,'retain','unknown',NULL)", (uid, uid))
    before = describe(conn, 'root', now=200)
    conn.execute("UPDATE kanban_failure_commands SET delivery_state='pending' WHERE run_uid='a'")
    after = describe(conn, 'root', now=300)
    assert before['contract_key'] == after['contract_key']
    assert after['evidence_state'] == 'unknown'
    assert after['release']['authority'] == 'unavailable'
    conn.close()
