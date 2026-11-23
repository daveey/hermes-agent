"""Episode/deadline boundaries from bounded slice review; synthetic diagnostic fixtures. [daveey]"""
from __future__ import annotations

import sqlite3
import json

import pytest

from hermes_cli.kanban_actionability import project
from hermes_cli.kanban_actionability_sources import describe


@pytest.mark.parametrize('value,expected', [
    (None, None), (True, None), (False, None), ('1', None), ([], None),
    (float('nan'), None), (float('inf'), None), (-1, None), (0, None),
    (1, 1), (1.9, 1),
])
def test_positive_time_rejects_nonfinite_and_nonnumeric_values(value, expected):
    from hermes_cli.kanban_actionability import positive_time
    assert positive_time(value) == expected


@pytest.mark.parametrize('value,kind,refs', [
    (None, 'unavailable', []), ('', 'unavailable', []), ('  ', 'unavailable', []),
    (17, 'unavailable', []), ('default', 'assignee', ['default']),
])
def test_reducer_owner_requires_nonblank_recorded_profile(value, kind, refs):
    from hermes_cli.kanban_actionability import owner
    assert owner(value) == {'kind': kind, 'refs': refs}


@pytest.mark.parametrize('wake,awaiting,probe,action,violation,trigger', [
    (None, {}, None, 'repair_lifecycle_contract', 'scheduled_wake_missing', 'Repair the missing'),
    (1000, {}, None, 'repair_lifecycle_contract', 'recovery_trigger_missing', 'Repair the missing'),
    (1000, {'kind': 'clock'}, None, 'wait_for_named_producer', None, 'Recorded clock reaches 1000'),
    (1000, {'kind': 'ci', 'ref': 'run-17'}, None, 'wait_for_named_producer', None, 'Recorded ci producer run-17'),
    (1000, {'kind': 'external', 'why': 'Vendor reply'}, None, 'wait_for_named_producer', None, 'Vendor reply'),
    (1000, {}, 'probe-ready', 'wait_for_named_producer', None, 'Recorded condition probe succeeds: probe-ready'),
])
def test_reducer_schedule_trigger_and_clock_boundaries(wake, awaiting, probe, action, violation, trigger):
    facts = {'task': {'id': 'scheduled-fixture', 'status': 'scheduled', 'assignee': 'default'},
             'wake_at': wake, 'awaiting': awaiting, 'probe': probe}
    row = project(facts, now=1000)
    assert row['action'] == action
    assert trigger in row['blocker']['trigger']
    assert row['blocker']['prerequisite'] == (
        'Named finite producer and recovery condition' if violation else row['blocker']['trigger'])
    assert ('scheduled_wake_missing' in row['violations']) == (violation == 'scheduled_wake_missing')
    assert ('recovery_trigger_missing' in row['violations']) == (violation is not None)
    assert row['escalation']['at'] == (1600 if wake else None)
    if wake:
        assert 'scheduled_wake_overdue' not in project(facts, now=1600)['violations']
        assert 'scheduled_wake_overdue' in project(facts, now=1601)['violations']


@pytest.mark.parametrize('evidence', [None, 'not-a-list', ['kept'], ['kept', 'kept'], [17]])
def test_reducer_retained_evidence_requires_string_list(evidence):
    facts = {'task': {'id': 'held-fixture', 'status': 'triage', 'assignee': 'default'},
             'source_refs': ['source'], 'stop': {'cause': 'scope_stopped'},
             'next_action': {'blocker': {'evidence': evidence}}}
    row = project(facts, now=100)
    expected = ['source', 'kept'] if isinstance(evidence, list) and evidence and isinstance(evidence[0], str) else ['source']
    assert row['blocker']['evidence'] == expected

@pytest.mark.parametrize('field,value', [
    ('prerequisite', None), ('prerequisite', 17), ('prerequisite', ''), ('prerequisite', '  '),
    ('trigger', None), ('trigger', 17), ('trigger', ''), ('trigger', '  '),
    ('instruction', None), ('instruction', 17), ('instruction', ''), ('instruction', '  '),
    ('release', None), ('release', 17), ('release', {}),
    ('release', {'condition': None}), ('release', {'condition': 17}),
    ('release', {'condition': ''}), ('release', {'condition': '  '}),
])
def test_decision_invalid_optional_fields_keep_safe_fallback(field, value):
    contract = {'blocker': {'prerequisite': 'Actual prerequisite', 'trigger': 'Actual trigger'}}
    if field in ('prerequisite', 'trigger'):
        contract['blocker'][field] = value
    else:
        contract[field] = value
    row = project({'task': {'id': 'r', 'status': 'triage', 'assignee': 'default'},
                   'deadline_at': 2000, 'stop': {'cause': 'scope_stopped'},
                   'next_action': contract}, now=1000)
    if field == 'prerequisite':
        assert row['blocker']['prerequisite'] == 'scope_stopped'
    elif field == 'trigger':
        expected = 'Actual prerequisite' if not value else 'Administrative owner records a scoped decision; comments, timers and notices do not release work'
        assert row['blocker']['trigger'] == expected
    elif field == 'instruction':
        assert row['instruction'] == 'Actual trigger'
    else:
        assert row['release']['condition'] == 'Existing authenticated lifecycle policy'
    assert row['release']['authority'] == 'unavailable'


def test_prerequisite_alone_is_the_recorded_trigger_and_valid_release_is_display_only():
    row = project({'task': {'id': 'r', 'status': 'triage', 'assignee': 'default'},
                   'deadline_at': 2000, 'stop': {'cause': 'scope_stopped'},
                   'next_action': {'blocker': {'prerequisite': 'Recorded prerequisite'},
                                   'release': {'condition': 'Recorded condition'}}}, now=1000)
    assert row['blocker']['trigger'] == row['instruction'] == 'Recorded prerequisite'
    assert row['release'] == {'authority': 'unavailable', 'condition': 'Recorded condition'}


@pytest.mark.parametrize('administrative_owner,unknown', [
    ({'kind': 'task', 'refs': ['carrier']}, False),
    ({'kind': 'task', 'refs': []}, True),
    ({'kind': 'unknown', 'refs': ['carrier']}, True),
])
@pytest.mark.parametrize('source_unknown', [False, True])
def test_known_carrier_deadline_does_not_mask_source_or_owner_uncertainty(administrative_owner, unknown, source_unknown):
    row = project({'task': {'id': 'r', 'status': 'triage', 'assignee': 'default'},
                   'deadline_at': 2000, 'administrative_owner': administrative_owner,
                   'unknown': source_unknown}, now=1000)
    assert row['evidence_state'] == ('unknown' if unknown or source_unknown else 'known')
    assert row['escalation']['state'] == 'pending'
    assert 'escalation_deadline_missing' not in row['violations']


@pytest.mark.parametrize('source_at,created,expected', [(200, 100, 200), (None, 100, 100), (None, None, None)])
def test_first_observation_is_recorded_source_then_creation_only(source_at, created, expected):
    row = project({'task': {'id': 'r', 'status': 'triage', 'assignee': 'default', 'created_at': created},
                   'source_at': source_at, 'deadline_at': 2000}, now=1000)
    assert row['first_observed'] == expected
    assert row['escalation']['at'] == 2000


@pytest.mark.parametrize('status', ['triage', 'scheduled'])
@pytest.mark.parametrize('now', [999, 1000, 1001])
def test_escalation_requires_blocker_and_recorded_deadline_inclusive(status, now):
    facts = {'task': {'id': 'r', 'status': status, 'assignee': 'default'}, 'deadline_at': 1000}
    row = project(facts, now=now)
    assert ('escalation_overdue' in row['violations']) == (status == 'scheduled' and now >= 1000)


@pytest.mark.parametrize('status', ['triage', 'scheduled'])
def test_wake_deadline_extension_is_scheduled_only(status):
    facts = {'task': {'id': 'r', 'status': status, 'assignee': 'default'},
             'wake_at': 1000, 'awaiting': {'kind': 'clock'}}
    row = project(facts, now=1000)
    assert row['escalation']['at'] == (1600 if status == 'scheduled' else None)


def test_pending_command_identity_is_material_and_delivery_state_is_not():
    facts = {'task': {'id': 'r', 'status': 'triage', 'assignee': 'default'},
             'pending_command': {'request_uid': 'one', 'request_sha256': 'hash', 'run_uid': 'run', 'operation_kind': 'stop'}}
    before = project(facts, now=1000)
    facts['pending_command']['delivery'] = 'confirmed'
    assert project(facts, now=1000)['contract_key'] == before['contract_key']
    facts['pending_command']['request_uid'] = 'two'
    assert project(facts, now=1000)['contract_key'] != before['contract_key']


@pytest.mark.parametrize('unknown', [False, True])
@pytest.mark.parametrize('status', ['done', 'archived'])
def test_terminal_unknown_evidence_is_preserved_without_open_action(unknown, status):
    row = project({'task': {'id': 'r', 'status': status}, 'unknown': unknown}, now=1000)
    assert row['evidence_state'] == ('unknown' if unknown else 'known')
    assert row['action'] == 'no_open_action'
    assert row['release']['authority'] == row['escalation']['state'] == 'not_applicable'


def test_clock_without_recorded_wake_never_claims_a_clock_deadline():
    row = project({'task': {'id': 'r', 'status': 'scheduled'},
                   'awaiting': {'kind': 'clock'}}, now=1000)
    assert row['blocker']['trigger'] == 'Recorded clock producer '
    assert row['escalation']['at'] is None
    assert row['action'] == 'repair_lifecycle_contract'


@pytest.mark.parametrize('limit', [True, False, 1.5, '2', None, 0, -1, 1001])
def test_report_limit_rejects_invalid_and_noninteger_inputs(limit):
    from hermes_cli.kanban_actionability_cli import report
    conn = fixture()
    with pytest.raises(ValueError, match='limit must be between'):
        report(conn, limit=limit, now=1000)
    conn.close()


@pytest.mark.parametrize('value,expected', [(None, {}), ('', {}), ('null', {}), ('[]', {}),
    ('{', {}), (17, {}), ('{"ok":1}', {'ok': 1}), ({'ok': 1}, {'ok': 1})])
def test_record_accepts_only_json_objects(value, expected):
    from hermes_cli.kanban_actionability_sources import record
    assert record(value) == expected


@pytest.mark.parametrize('field', ['action', 'why_human', 'scope', 'evidence', 'interaction'])
@pytest.mark.parametrize('value', [None, '', '  ', 17])
def test_human_step_requires_all_recorded_fields(field, value):
    from hermes_cli.kanban_actionability_sources import _human_step
    request = {'action': 'Recorded action', 'why_human': 'Reason', 'scope': 'Scope',
               'evidence': 'Evidence', 'interaction': 'reply'}
    assert _human_step(json.dumps(request)) == 'Recorded action'
    request[field] = value
    assert _human_step(json.dumps(request)) is None


@pytest.mark.parametrize('delivery,selected', [(None, True), ('unknown', True), ('pending', True),
                                              ('confirmed', False), ('rejected', False)])
def test_pending_reader_filters_and_orders_original_custody(delivery, selected):
    from hermes_cli.kanban_actionability_sources import Reader
    conn = fixture()
    conn.executescript('CREATE TABLE kanban_failure_commands(task_id TEXT,run_uid TEXT,request_uid TEXT,'
                       'operation_kind TEXT,delivery_state TEXT,canonical_receipt_json TEXT);')
    for task, run, request, operation in [('root', 'b', 'b', 'retain'), ('root', 'a', 'b', 'retain'),
                                         ('root', 'a', 'a', 'retain'), ('owner', '0', '0', 'retain'),
                                         ('root', '0', '0', 'stop')]:
        conn.execute('INSERT INTO kanban_failure_commands VALUES(?,?,?,?,?,NULL)',
                     (task, run, request, operation, delivery))
    row = Reader(conn).pending('root')
    if selected:
        assert row['run_uid'] == row['request_uid'] == 'a'
        assert row['_ambiguous'] is True
        conn.execute("DELETE FROM kanban_failure_commands WHERE run_uid='b' OR request_uid='b'")
        assert Reader(conn).pending('root')['_ambiguous'] is False
    else:
        assert row is None
    conn.close()


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('owned', [False, True])
def test_report_preserves_caller_transaction_even_on_failure(monkeypatch, fail, owned):
    from hermes_cli import kanban_actionability_cli as cli
    conn = fixture()
    conn.commit()
    if not owned:
        conn.execute("UPDATE tasks SET created_at=99 WHERE id='root'")
    if fail:
        def broken(*args, **kwargs):
            raise ValueError('snapshot failure')
        monkeypatch.setattr(cli, 'describe', broken)
        with pytest.raises(ValueError, match='snapshot failure'):
            cli.report(conn, now=1000)
    else:
        assert cli.report(conn, now=1000)['scanned_count'] == 2
    assert conn.in_transaction is (not owned)
    assert conn.execute("SELECT created_at FROM tasks WHERE id='root'").fetchone()[0] == (100 if owned else 99)
    conn.close()


def test_report_streams_multiple_batches_with_exact_pagination():
    from hermes_cli.kanban_actionability_cli import report
    conn = fixture()
    conn.execute('DELETE FROM tasks')
    conn.executemany("INSERT INTO tasks VALUES(?,'triage','default',100)", [(f'{i:04}',) for i in range(260)])
    conn.commit()
    result = report(conn, limit=1, after='0258', now=1000)
    assert result['scanned_count'] == result['total_nonterminal'] == 260
    assert result['per_status'] == {'triage': 260}
    assert result['returned_count'] == 1
    assert result['rows'][0]['task_id'] == '0259'
    assert result['has_more'] is False and result['next_after'] is None
    result = report(conn, limit=1, now=1000)
    assert result['has_more'] is True and result['next_after'] == '0000'
    conn.close()


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
