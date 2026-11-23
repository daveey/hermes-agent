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


@pytest.mark.parametrize('status', ['triage', 'done', 'archived'])
@pytest.mark.parametrize('replica', [False, True])
def test_fields_replica_changes_only_open_known_evidence(monkeypatch, status, replica):
    from types import SimpleNamespace
    from hermes_cli import kanban_actionability_sources as sources
    from hermes_cli.kanban_actionability_evidence import fields
    task = SimpleNamespace(id='r', status=status, assignee='default')
    monkeypatch.setattr(sources, 'describe', lambda *a, **k: project({
        'task': {'id': 'r', 'status': status, 'assignee': 'default'}, 'deadline_at': 500}, now=100))
    row = fields(None, task, replica=replica)['actionability']
    assert row['evidence_state'] == ('unknown' if replica and status == 'triage' else 'known')


@pytest.mark.parametrize('violations,text', [([], 'none observed'), (['one', 'two'], 'one,two')])
def test_render_preserves_exact_diagnostic_list_and_bound(violations, text):
    from hermes_cli.kanban_actionability_evidence import render
    row = project({'task': {'id': 'r', 'status': 'done'}}, now=100)
    row['violations'] = violations
    assert render(row).endswith('violations=' + text)
    row['instruction'] = 'x' * 3000
    assert len(render(row)) == 1800


@pytest.mark.parametrize('cause', ['scope_stopped', 'safety_refusal'])
def test_effective_policy_adapts_actual_restriction_cause(cause):
    from hermes_cli.kanban_actionability_sources import Reader
    from hermes_cli.kanban_actionability_evidence import effective
    conn = complete_fixture()
    event(conn, 's', 'spend_budget_triaged', {'cause': cause}, 100)
    stop, restricted, unknown = effective(Reader(conn), 'root')
    assert restricted is True and unknown is False
    assert stop is not None
    assert stop['data']['cause'] == cause
    conn.close()


def complete_fixture():
    """Small complete local read schema: unknown predicates cannot hide each other."""
    conn = fixture()
    conn.executescript('ALTER TABLE tasks ADD COLUMN current_run_id INTEGER;'
        'ALTER TABLE tasks ADD COLUMN containment_episode TEXT;'
        'ALTER TABLE tasks ADD COLUMN human_request TEXT;'
        'ALTER TABLE task_events ADD COLUMN run_id INTEGER;'
        'ALTER TABLE task_events ADD COLUMN deleted_at INTEGER;'
        'CREATE TABLE task_runs(id INTEGER,uid TEXT,task_id TEXT,status TEXT,ended_at INTEGER);'
        'CREATE TABLE task_wake_checks(task_id TEXT,cmd TEXT);'
        'CREATE TABLE kanban_failure_commands(task_id TEXT,run_uid TEXT,request_uid TEXT,'
        'operation_kind TEXT,delivery_state TEXT,canonical_receipt_json TEXT);')
    return conn


def held_fixture():
    conn = complete_fixture()
    event(conn, 'stop', 'spend_budget_triaged', {'cause': 'scope_stopped'}, 100)
    event(conn, 'receipt', 'decision_retained', {'episode_uid': 'stop', 'next_action': {
        'owner': {'kind': 'task', 'task_id': 'owner'}, 'instruction': 'Original instruction',
        'escalate_at': 500}}, 101)
    row = describe(conn, 'root', now=200)
    assert row['evidence_state'] == 'known', row
    assert row['owner'] == {'kind': 'task', 'refs': ['owner']}
    assert row['escalation']['at'] == 500
    return conn


@pytest.mark.parametrize('damage', ['missing_links', 'missing_runs', 'missing_wake', 'missing_commands',
    'partial_sync', 'active_sync', 'bad_episode', 'bad_cause', 'bad_next_action', 'bad_producer',
    'missing_uid', 'invalid_human'])
def test_each_source_uncertainty_independently_changes_known_baseline(damage):
    conn = held_fixture()
    if damage.startswith('missing_') and damage != 'missing_uid':
        table = {'missing_links': 'task_links', 'missing_runs': 'task_runs',
                 'missing_wake': 'task_wake_checks', 'missing_commands': 'kanban_failure_commands'}[damage]
        conn.execute('DROP TABLE ' + table)
    elif damage == 'partial_sync':
        conn.execute('CREATE TABLE kanban_sync_state(board_id TEXT)')
    elif damage == 'active_sync':
        conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
    elif damage == 'missing_uid':
        conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
        conn.execute('UPDATE task_events SET uid=NULL')
    elif damage == 'invalid_human':
        conn.execute("UPDATE tasks SET human_request='{}' WHERE id='root'")
    else:
        field = {'bad_episode': 'episode', 'bad_cause': 'cause', 'bad_next_action': 'next_action',
                 'bad_producer': 'producer'}[damage]
        event(conn, 'damaged', 'created', {field: []}, 102)
    assert describe(conn, 'root', now=200)['evidence_state'] == 'unknown'
    conn.close()


@pytest.mark.parametrize('raw', [None, ''])
def test_empty_event_payload_is_valid_not_malformed(raw):
    from hermes_cli.kanban_actionability_sources import Reader
    conn = complete_fixture()
    event(conn, 'created', 'created', {}, 100)
    conn.execute('UPDATE task_events SET payload=?', (raw,))
    reader = Reader(conn)
    assert reader.events('root')[0]['data'] == {}
    assert reader.malformed == set()
    conn.close()


def test_event_reader_uses_uid_tiebreak_and_excludes_deleted_rows():
    from hermes_cli.kanban_actionability_sources import Reader
    conn = complete_fixture()
    for uid in ('z', 'a', 'deleted'):
        event(conn, uid, 'created', {}, 100)
    conn.execute("UPDATE task_events SET deleted_at=1 WHERE uid='deleted'")
    assert [e['uid'] for e in Reader(conn).events('root')] == ['z', 'a']
    conn.close()


@pytest.mark.parametrize('mode,synced', [(None, False), ('off', False), ('primary', True)])
def test_sync_reader_distinguishes_empty_off_and_active(mode, synced):
    from hermes_cli.kanban_actionability_sources import Reader
    conn = complete_fixture()
    conn.execute('CREATE TABLE kanban_sync_state(mode TEXT)')
    if mode is not None:
        conn.execute('INSERT INTO kanban_sync_state VALUES(?)', (mode,))
    reader = Reader(conn)
    assert reader.synced is synced
    assert reader.partial is False
    conn.close()


def test_same_episode_deadlines_minimum_and_latest_contract_survive_producer_notice():
    conn = held_fixture()
    event(conn, 'later', 'decision_retained', {'episode': 'stop', 'next_action': {
        'owner': {'kind': 'task', 'refs': ['owner']}, 'instruction': 'Newest contract',
        'escalation': {'at': 800}}}, 102)
    event(conn, 'foreign', 'decision_retained', {'episode': 'foreign', 'next_action': {
        'instruction': 'Wrong episode', 'escalate_at': 50}}, 103)
    event(conn, 'notice', 'spend_budget_decision_ready', {'episode': 'stop',
        'producer': {'kind': 'task', 'task_id': 'owner'}}, 104)
    row = describe(conn, 'root', now=200)
    assert row['instruction'] == 'Newest contract'
    assert row['escalation']['at'] == 500
    assert row['owner'] == {'kind': 'task', 'refs': ['owner']}
    assert row['first_observed'] == 100
    conn.close()


@pytest.mark.parametrize('route,kind,refs', [
    ({'kind': 'task', 'refs': ['owner']}, 'task', ['owner']),
    ({'kind': 'chat'}, 'chat', []), ({'kind': 'chats'}, 'chat', []),
    ({'kind': 'task', 'refs': 'owner'}, 'unavailable', []),
    ({'kind': 'task', 'refs': [17]}, 'unavailable', []),
    ({'task_id': 'missing'}, 'unavailable', ['missing']),
    ({'kind': 'task', 'refs': []}, 'unavailable', []),
])
def test_route_owner_exact_fallback_and_invalid_identity(route, kind, refs):
    from hermes_cli.kanban_actionability_sources import Reader, _route_owner
    conn = complete_fixture()
    assert _route_owner(Reader(conn), route) == {'kind': kind, 'refs': refs}
    conn.close()


@pytest.mark.parametrize('damage,kind', [('done', 'unavailable'), ('archived', 'unavailable'),
    ('contained', 'unavailable'), ('scope', 'unavailable'), ('malformed', 'unknown'), ('synced', 'unknown')])
def test_carrier_availability_is_only_administrative(damage, kind):
    from hermes_cli.kanban_actionability_sources import Reader, _route_owner
    conn = complete_fixture()
    if damage in ('done', 'archived'):
        conn.execute("UPDATE tasks SET status=? WHERE id='owner'", (damage,))
    elif damage == 'contained':
        conn.execute("UPDATE tasks SET containment_episode='held' WHERE id='owner'")
    elif damage == 'synced':
        conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
    else:
        conn.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES('s','owner','spend_budget_triaged',?,100)",
                     ('[]' if damage == 'malformed' else '{"cause":"scope_stopped"}',))
    assert _route_owner(Reader(conn), {'task_id': 'owner'}) == {'kind': kind, 'refs': ['owner']}
    conn.close()


@pytest.mark.parametrize('status,ended,exists', [('running', None, True), ('failed', None, True),
                                              ('running', 100, True), ('running', None, False)])
def test_current_run_evidence_requires_matching_live_row(status, ended, exists):
    conn = complete_fixture()
    conn.execute("UPDATE tasks SET status='running',current_run_id=1 WHERE id='root'")
    if exists:
        conn.execute("INSERT INTO task_runs VALUES(1,'r','root',?,?)", (status, ended))
    row = describe(conn, 'root', now=200)
    assert ('running_without_current_run' in row['violations']) is not (exists and status == 'running' and ended is None)
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


# ---------------------------------------------------------------------------
# Source/evidence mutant regression additions (append to the contract test).
# Reuse complete_fixture / held_fixture / event / fixture defined above.
# ---------------------------------------------------------------------------

def _ev(conn, task, uid, kind, data, at):
    conn.execute('INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES(?,?,?,?,?)',
                 (uid, task, kind, json.dumps(data), at))


def _reader(conn):
    from hermes_cli.kanban_actionability_sources import Reader
    return Reader(conn)


def _stub_policy(monkeypatch, stop, restricted, safety=False):
    from hermes_cli import kanban_spend_decision as decision, kanban_refusal_receipt as refusal
    monkeypatch.setattr(decision, 'pending_episode', lambda conn, tid: stop)
    monkeypatch.setattr(decision, 'execution_restricted', lambda conn, tid: restricted)
    monkeypatch.setattr(refusal, 'restricted', lambda conn, tid: safety)


@pytest.mark.parametrize('payload,restricted,safety,expect', [
    ('{}', False, False, None),                       # M3223: unrestricted: no cause invented
    ('{"cause":"custom"}', False, False, 'custom'),  # M3223: unrestricted keeps cause
    ('{"cause":"custom"}', True, False, 'scope_stopped'),  # M3224: restriction overwrites
    ('{"cause":"custom"}', True, True, 'safety_refusal'),
])
def test_effective_overwrites_cause_only_for_restricted_stop(monkeypatch, payload, restricted, safety, expect):
    from hermes_cli.kanban_actionability_evidence import effective
    _stub_policy(monkeypatch, {'id': 1, 'payload': payload}, restricted, safety)
    stop, got_restricted, unknown = effective(_reader(complete_fixture()), 'root')
    assert (got_restricted, unknown) == (restricted, False)
    assert stop['data'].get('cause') == expect


@pytest.mark.parametrize('raw', [None, ''])
def test_effective_blank_stop_payload_is_empty_known_data(monkeypatch, raw):
    from hermes_cli.kanban_actionability_evidence import effective
    _stub_policy(monkeypatch, {'id': 1, 'payload': raw}, False)
    stop, restricted, unknown = effective(_reader(complete_fixture()), 'root')
    assert stop['data'] == {} and (restricted, unknown) == (False, False)


@pytest.mark.parametrize('payload', ['[]', '17', 'not json'])
def test_effective_non_object_or_bad_stop_payload_is_unknown_without_stop(monkeypatch, payload):
    from hermes_cli.kanban_actionability_evidence import effective
    _stub_policy(monkeypatch, {'id': 1, 'payload': payload}, True)
    assert effective(_reader(complete_fixture()), 'root') == (None, False, True)


def test_describe_unknown_task_raises_value_error_not_type_error():
    conn = complete_fixture()
    with pytest.raises(ValueError, match='unknown task: nope'):
        describe(conn, 'nope', now=1)
    conn.close()


@pytest.mark.parametrize('columns', [None, 'task_id TEXT,run_uid TEXT,request_uid TEXT,operation_kind TEXT'])
def test_pending_missing_table_or_required_columns_returns_none_without_sql(columns):
    conn = fixture()
    if columns:
        conn.execute(f'CREATE TABLE kanban_failure_commands({columns})')
    statements = []
    conn.set_trace_callback(statements.append)
    assert _reader(conn).pending('root') is None
    assert not any('FROM kanban_failure_commands' in s for s in statements)
    conn.close()


def test_orphan_parent_has_named_recovery_trigger_and_no_status():
    from hermes_cli.kanban_actionability_sources import _dependencies
    conn = complete_fixture()
    conn.execute("INSERT INTO task_links VALUES('ghost','root')")
    deps, unknown = _dependencies(_reader(conn), 'root')
    assert unknown is True
    assert deps == [{'task_id': 'ghost', 'status': None, 'owner': {'kind': 'unavailable', 'refs': []},
                     'trigger': 'Recover authoritative parent evidence for ghost'}]
    conn.close()


@pytest.mark.parametrize('status,open_', [('triage', True), ('done', False), ('archived', False)])
def test_describe_evidence_fault_is_repair_for_open_and_preserved_for_terminal(monkeypatch, status, open_):
    from hermes_cli import kanban_actionability_sources as sources
    conn = complete_fixture()
    conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, 'root'))
    def boom(*a, **k):
        raise sqlite3.OperationalError('boom')
    monkeypatch.setattr(sources, '_describe', boom)
    row = describe(conn, 'root', now=200)
    assert row['evidence_state'] == 'unknown'
    assert row['action'] == ('repair_lifecycle_contract' if open_ else 'no_open_action')
    assert row['violations'] == (['evidence_unavailable'] if open_ else [])
    assert (row['instruction'] == 'Recover authoritative task evidence') is open_
    conn.close()


def test_describe_now_default_uses_wall_clock_for_finite_deadline(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = held_fixture()   # escalate_at=500
    monkeypatch.setattr(sources.time, 'time', lambda: 400.9)
    assert 'escalation_overdue' not in describe(conn, 'root')['violations']
    monkeypatch.setattr(sources.time, 'time', lambda: 500.9)
    assert 'escalation_overdue' in describe(conn, 'root')['violations']
    conn.close()


@pytest.mark.parametrize('events,expect', [
    ([('ordinary', 'decision_retained', {})], 'ordinary'),
    ([('promo', 'promoted_manual', {}), ('ordinary', 'decision_retained', {})], None),
    ([('promo', 'promoted_manual', {}), ('p', 'decision_protected_stopped', {'disposition': {}})], 'p'),
    ([('promo', 'promoted_manual', {}),
      ('ordinary', 'decision_retained', {'superseded_by_safety_stop': True})], 'ordinary'),
    ([('promo', 'promoted_manual', {}),
      ('ordinary', 'decision_retained', {'disposition': {'reason': 'scope_stopped'}})], 'ordinary'),
    ([('promo', 'promoted_manual', {}),
      ('ordinary', 'decision_retained', {'superseded_by_safety_stop': 1})], None),
])
def test_unresolved_receipt_promotion_matrix_newest_first(events, expect):
    from hermes_cli.kanban_actionability_sources import _unresolved_receipt
    got = _unresolved_receipt([{'uid': u, 'kind': k, 'data': d} for u, k, d in events])
    assert (got or {}).get('uid') == expect


def test_state_unconfirmed_ordinary_receipt_alone_makes_unknown():
    from hermes_cli.kanban_actionability_sources import _state
    conn = complete_fixture()
    reader = _reader(conn)
    assert reader.partial is False
    assert _state(reader, 'root', reader.events('root'))[2] is False
    event(conn, 'receipt', 'decision_retained', {'episode_uid': 'old'}, 100)
    stop, restricted, unknown = _state(reader, 'root', reader.events('root'))
    assert stop['uid'] == 'receipt' and restricted is False and unknown is True
    conn.close()


def _stub_renewal(monkeypatch, covered=(), valid=()):
    from hermes_cli import kanban_scope_renewal as renewal
    monkeypatch.setattr(renewal, 'covered', lambda conn, tid: frozenset(covered))
    monkeypatch.setattr(renewal, 'renewals', lambda conn, tid: list(valid))


def _receipt(uid, row_id, episode='ep', **data):
    return {'id': row_id, 'uid': uid, 'kind': 'decision_retained', 'data': {'episode_uid': episode, **data}}


def test_state_policy_error_without_receipts_is_unknown(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    monkeypatch.setattr(sources, 'effective', lambda reader, tid: (None, False, True))
    reader = _reader(complete_fixture())
    assert sources._state(reader, 'root', []) == (None, False, True)


def test_state_renewal_seam_failure_is_unknown_and_drops_coverage(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources, kanban_scope_renewal as renewal
    monkeypatch.setattr(sources, 'effective', lambda reader, tid: (None, False, False))
    def boom(*a):
        raise sqlite3.OperationalError('renewal')
    monkeypatch.setattr(renewal, 'covered', boom)
    reader = _reader(complete_fixture())
    stop, restricted, unknown = sources._state(reader, 'root', [])
    assert (stop, restricted, unknown) == (None, False, True)


@pytest.mark.parametrize('case,survives', [
    ('covered_ordinary', False),       # M3475
    ('covered_but_safety', True),      # M3438
    ('older_other_episode', True),     # M3529
    ('newer_same_episode', True),      # M3530
    ('older_same_episode', False),
    ('uncovered', True),
])
def test_state_renewal_filters_only_covered_or_prior_same_episode_ordinary_receipts(monkeypatch, case, survives):
    from hermes_cli import kanban_actionability_sources as sources
    monkeypatch.setattr(sources, 'effective', lambda reader, tid: (None, False, False))
    receipt = _receipt('r', 11 if case == 'newer_same_episode' else 5,
                       episode='other' if case == 'older_other_episode' else 'ep')
    covered = {'r'} if case in ('covered_ordinary', 'covered_but_safety') else ()
    if case == 'covered_but_safety':
        receipt['data']['superseded_by_safety_stop'] = True
    valid = [{'id': 10, 'payload': {'episode': 'ep'}}] if case in (
        'older_other_episode', 'newer_same_episode', 'older_same_episode') else []
    _stub_renewal(monkeypatch, covered, valid)
    reader = _reader(complete_fixture())
    stop, _, unknown = sources._state(reader, 'root', [receipt])
    assert (stop is not None) is survives and unknown is survives


def _dep_state(conn, parent_status='done'):
    from hermes_cli.kanban_actionability_sources import _dependencies
    conn.execute("UPDATE tasks SET status=? WHERE id='owner'", (parent_status,))
    conn.execute("INSERT INTO task_links VALUES('owner','root')")
    return _dependencies(_reader(conn), 'root')


@pytest.mark.parametrize('damage,kept,dep_unknown', [
    (None, False, False),
    ('stop', True, False),                 # M3366/M3367
    ('contained', True, False),            # M3368
    ('partial_policy', True, True),        # M3363/M3501: parent unknown keeps and flags row
    ('synced_stop', True, True),           # M3501 dependency-level unknown for sync
])
def test_terminal_parent_is_omitted_only_when_fully_known_and_uncontained(damage, kept, dep_unknown):
    conn = complete_fixture()
    if damage == 'contained':
        conn.execute("UPDATE tasks SET containment_episode='held' WHERE id='owner'")
    if damage == 'partial_policy':
        _ev(conn, 'owner', 'bad', 'created', {'episode': []}, 100)
    if damage == 'synced_stop':
        conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
    if damage in ('stop', 'synced_stop'):
        _ev(conn, 'owner', 'ps', 'spend_budget_triaged', {'cause': 'safety_refusal'}, 100)
    deps, unknown = _dep_state(conn)
    assert bool(deps) is kept
    if kept:
        assert deps[0]['evidence_state'] == ('unknown' if dep_unknown else 'known')
        assert unknown is dep_unknown
    conn.close()


def test_parent_partial_schema_alone_marks_unknown_and_keeps_terminal_parent():
    conn = fixture()           # legacy fixture: optional tables absent => reader.partial
    conn.execute("UPDATE tasks SET status='done' WHERE id='owner'")
    conn.execute("INSERT INTO task_links VALUES('owner','root')")
    from hermes_cli.kanban_actionability_sources import _dependencies
    reader = _reader(conn)
    assert reader.partial and not reader.synced
    deps, unknown = _dependencies(reader, 'root')
    assert unknown is True and deps[0]['evidence_state'] == 'unknown'
    conn.close()


@pytest.mark.parametrize('data,expect', [
    ({'episode_uid': 'EU'}, 'EU'), ({'episode': 'EP'}, 'EP'),
    ({'episode_uid': 'EU', 'episode': 'EP'}, 'EU'), ({}, 'stop-uid'),
])
def test_parent_stop_episode_and_scoped_administrative_owner(data, expect):
    conn = complete_fixture()
    _ev(conn, 'owner', 'stop-uid', 'spend_budget_triaged', {'cause': 'scope_stopped', **data}, 100)
    _ev(conn, 'owner', 'scoped', 'decision_retained', {'episode_uid': expect, 'producer': {'task_id': 'root'}}, 101)
    _ev(conn, 'owner', 'foreign', 'decision_retained', {'episode_uid': 'other', 'producer': {'task_id': 'ghost'}}, 102)
    conn.execute("UPDATE tasks SET status='todo' WHERE id='owner'")
    deps, _ = _dep_state(conn, 'todo')
    assert deps[0]['stop_episode'] == expect
    # only the producer scoped to the stop's own episode supplies the owner
    assert deps[0]['administrative_owner'] == {'kind': 'task', 'refs': ['root']}
    conn.close()


@pytest.mark.parametrize('stop,contained,human,trigger', [
    (False, None, None, 'owner: accountable owner completes its acceptance'),
    (True, None, None, 'owner: supported resolution of its protected episode'),
    (False, 'held', None, 'owner: supported resolution of its protected episode'),
    (False, None, {'action': 'Reply to vendor', 'why_human': 'w', 'scope': 's', 'evidence': 'e',
                   'interaction': 'reply'}, 'owner: Reply to vendor'),
    (True, 'held', {'action': 'Reply to vendor', 'why_human': 'w', 'scope': 's', 'evidence': 'e',
                    'interaction': 'reply'}, 'owner: Reply to vendor'),
])
def test_parent_trigger_wording_is_exact(stop, contained, human, trigger):
    conn = complete_fixture()
    conn.execute("UPDATE tasks SET containment_episode=?,human_request=? WHERE id='owner'",
                 (contained, json.dumps(human) if human else None))
    if stop:
        _ev(conn, 'owner', 's', 'spend_budget_triaged', {'cause': 'scope_stopped'}, 100)
    deps, _ = _dep_state(conn, 'todo')
    assert deps[0]['trigger'] == trigger
    conn.close()


@pytest.mark.parametrize('route,expect', [
    ({'task_id': 'owner', 'kind': 'task', 'refs': ['root']}, {'kind': 'task', 'refs': ['owner']}),   # M3283
    ({'kind': 'chat', 'refs': ['owner']}, {'kind': 'chat', 'refs': []}),                              # M3284
    ({'kind': 'task', 'refs': ['owner', 'root']}, {'kind': 'task', 'refs': ['owner']}),
])
def test_route_owner_prefers_task_id_and_never_resolves_chat_refs(route, expect):
    from hermes_cli.kanban_actionability_sources import _route_owner
    conn = complete_fixture()
    conn.execute("UPDATE tasks SET status='done' WHERE id='root'")   # root unusable, owner usable
    assert _route_owner(_reader(conn), route) == expect
    conn.close()


def test_route_owner_ordinary_budget_stop_carrier_is_unavailable_not_task():
    from hermes_cli.kanban_actionability_sources import _route_owner
    conn = complete_fixture()
    _ev(conn, 'owner', 'ord', 'spend_budget_triaged', {'cause': 'spend_budget'}, 100)
    assert _route_owner(_reader(conn), {'task_id': 'owner'}) == {'kind': 'unavailable', 'refs': ['owner']}
    conn.close()


def test_scheduled_probe_is_exact_recorded_condition():
    conn = complete_fixture()
    conn.execute("UPDATE tasks SET status='scheduled' WHERE id='root'")
    event(conn, 'sch', 'scheduled', {'wake_at': 1000, 'awaiting': {}}, 100)
    conn.execute("INSERT INTO task_wake_checks VALUES('root','probe-cmd')")
    row = describe(conn, 'root', now=200)
    assert row['blocker']['trigger'] == 'Recorded condition probe succeeds: probe-cmd'
    assert row['action'] == 'wait_for_named_producer'
    conn.close()


@pytest.mark.parametrize('damage', ['runs_incomplete', 'tasks_lacks_current_run_id'])
def test_incomplete_run_schema_never_asserts_running_without_current_run(damage):
    conn = complete_fixture()
    conn.execute("UPDATE tasks SET status='running',current_run_id=1 WHERE id='root'")
    if damage == 'runs_incomplete':
        conn.executescript('DROP TABLE task_runs; CREATE TABLE task_runs(id INTEGER,task_id TEXT);')
    else:
        conn.executescript("ALTER TABLE tasks DROP COLUMN current_run_id;")
    statements = []
    conn.set_trace_callback(statements.append)
    row = describe(conn, 'root', now=200)
    assert 'running_without_current_run' not in row['violations']
    assert row['violations'] != ['evidence_unavailable']
    assert not any('FROM task_runs' in s for s in statements)
    conn.close()


def test_stop_beats_newer_material_and_root_timestamp_is_episode_origin():
    conn = complete_fixture()
    event(conn, 'origin', 'created', {}, 50)
    event(conn, 'stop', 'spend_budget_triaged', {'cause': 'spend_budget', 'episode': 'origin'}, 100)
    event(conn, 'newer', 'blocked', {}, 200)
    row = describe(conn, 'root', now=300)
    assert row['source_refs'] == ['stop']
    assert row['first_observed'] == 50
    conn.close()


@pytest.mark.parametrize('status,expected_refs', [('scheduled', ['sched']), ('triage', ['newer-block'])])
def test_material_source_selection_for_scheduled_versus_other_tasks(status, expected_refs):
    conn = complete_fixture()
    conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, 'root'))
    event(conn, 'sched', 'scheduled', {'wake_at': 1000, 'awaiting': {'kind': 'clock'}}, 150)
    event(conn, 'newer-block', 'blocked', {}, 160)
    event(conn, 'notice', 'spend_budget_decision_ready', {}, 170)
    row = describe(conn, 'root', now=300)
    assert row['source_refs'] == expected_refs
    assert row['first_observed'] == (150 if status == 'scheduled' else 160)
    conn.close()


def test_ordinary_material_age_differs_from_task_creation():
    conn = complete_fixture()
    event(conn, 'blk', 'blocked', {}, 333)
    row = describe(conn, 'root', now=400)
    assert row['source_refs'] == ['blk'] and row['first_observed'] == 333
    conn.close()


def test_promotion_between_receipt_and_notice_yields_no_stop():
    conn = complete_fixture()
    event(conn, 'receipt', 'decision_retained', {'episode_uid': 'x'}, 100)
    event(conn, 'promo', 'promoted_manual', {}, 101)
    event(conn, 'notice', 'spend_budget_decision_ready', {}, 102)
    row = describe(conn, 'root', now=300)
    assert row['source_refs'] == ['promo'] and row['first_observed'] == 101
    assert row['escalation']['at'] is None
    conn.close()


@pytest.mark.parametrize('episode', [42, True, ['x']])
def test_nonstring_stop_episode_is_not_material_identity(monkeypatch, episode):
    from hermes_cli import kanban_actionability_sources as sources
    seen = {}
    monkeypatch.setattr(sources, "project", lambda facts, now: seen.update(facts) or project(facts, now=now))
    conn = complete_fixture()
    event(conn, 'stop', 'spend_budget_triaged', {'cause': 'spend_budget'}, 100)
    base = describe(conn, 'root', now=200)
    conn.execute('UPDATE task_events SET payload=?', (json.dumps({'cause': 'spend_budget', 'episode': episode}),))
    row = describe(conn, 'root', now=200)
    assert row['evidence_state'] == 'unknown'
    assert seen['stop_episode'] is None
    conn.execute('UPDATE task_events SET payload=?', (json.dumps({'cause': 'spend_budget'}),))
    assert describe(conn, 'root', now=200)['contract_key'] == base['contract_key']
    conn.close()


def test_stop_with_only_episode_uid_uses_it_as_episode_root():
    conn = complete_fixture()
    event(conn, 'origin', 'created', {}, 40)
    event(conn, 'stop', 'spend_budget_triaged', {'episode_uid': 'origin'}, 100)
    row = describe(conn, 'root', now=300)
    assert row['first_observed'] == 40
    conn.close()


@pytest.mark.parametrize('cause,expected', [('custom_cause', 'custom_cause'), (None, 'spend_budget'),
                                            (17, 'spend_budget'), ('  ', 'spend_budget')])
def test_ordinary_stop_cause_is_preserved_or_defaults_to_spend_budget(monkeypatch, cause, expected):
    from hermes_cli import kanban_actionability_sources as sources
    seen = {}
    monkeypatch.setattr(sources, 'project', lambda facts, now: seen.update(facts) or project(facts, now=now))
    conn = complete_fixture()
    payload = {} if cause is None else {'cause': cause}
    event(conn, 'stop', 'spend_budget_triaged', payload, 100)
    monkeypatch.setattr(sources, 'effective', lambda reader, tid: (
        {**reader.events(tid)[0], 'data': payload}, False, False))
    sources.describe(conn, 'root', now=200)
    assert seen['stop']['cause'] == expected
    conn.close()


def test_administration_none_episode_ignores_scoped_contracts():
    from hermes_cli.kanban_actionability_sources import _administration
    conn = complete_fixture()
    event(conn, 'a', 'decision_retained', {'episode': 'x', 'next_action': {'instruction': 'i', 'escalate_at': 9}}, 100)
    admin, contract = _administration(_reader(conn), _reader(conn).events('root'), None)
    assert admin == {'kind': 'unavailable', 'refs': []} and contract == {'escalate_at': None}
    conn.close()


def test_nested_only_deadline_populates_administration_and_fractional_edge():
    from hermes_cli.kanban_actionability_sources import _administration
    conn = complete_fixture()
    event(conn, 's', 'spend_budget_triaged', {'cause': 'scope_stopped'}, 100)
    event(conn, 'n', 'decision_retained', {'episode_uid': 's', 'next_action': {'escalation': {'at': 800}}}, 101)
    reader = _reader(conn)
    assert _administration(reader, reader.events('root'), 's')[1]['escalate_at'] == 800
    assert describe(conn, 'root', now=200)['escalation']['at'] == 800
    conn.close()


def test_valid_human_request_on_known_held_fixture_keeps_evidence_known():
    conn = held_fixture()
    conn.execute("UPDATE tasks SET human_request=? WHERE id='root'", (json.dumps(
        {'action': 'Reply', 'why_human': 'w', 'scope': 's', 'evidence': 'e', 'interaction': 'reply'}),))
    row = describe(conn, 'root', now=200)
    assert row['evidence_state'] == 'known'
    conn.close()


def test_active_sync_on_known_held_fixture_marks_unknown_and_top_level_only_via_sync():
    conn = held_fixture()
    conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
    assert describe(conn, 'root', now=200)['evidence_state'] == 'unknown'
    conn.close()




def test_decision_fallback_and_stop_cause_material_identity():
    facts = {'task': {'id': 'r', 'status': 'triage', 'assignee': 'default'},
             'deadline_at': 2000, 'stop': {'episode': 'e'}}
    assert project(facts, now=1000)['blocker']['prerequisite'] == 'Recorded spend decision'
    keys = []
    for stop in ({'cause': 'scope_stopped'}, {'cause': 'safety_refusal'}, None):
        facts['stop'] = stop
        keys.append(project(facts, now=1)['contract_key'])
    assert len(set(keys)) == 3


@pytest.mark.parametrize('deadline,expected', [(None, 1600), (1200, 1600), (5000, 5000)])
def test_scheduled_deadline_preserves_later_recorded_deadline(deadline, expected):
    row = project({'task': {'id': 'r', 'status': 'scheduled', 'assignee': 'default'},
                   'wake_at': 1000, 'awaiting': {'kind': 'clock'}, 'deadline_at': deadline}, now=1000)
    assert row['escalation']['at'] == expected


@pytest.mark.parametrize('restricted,authority,incident', [(False, 'existing_lifecycle_gates', False),
                                                         (True, 'unavailable', True)])
def test_dependency_incident_requires_restricted_parent(restricted, authority, incident):
    dep = {'task_id': 'p', 'status': 'todo', 'evidence_state': 'known', 'restricted': restricted,
           'administrative_owner': {'kind': 'unavailable', 'refs': []}, 'trigger': 't'}
    row = project({'task': {'id': 'r', 'status': 'todo', 'assignee': 'default'},
                   'deadline_at': 2000, 'dependencies': [dep]}, now=1000)
    assert row['release']['authority'] == authority
    assert ('dependency_recovery_unavailable' in row['violations']) is incident


@pytest.mark.parametrize('status,action', [('ready', 'execute_current_scope'), ('review', 'review_current_scope'),
                                         ('todo', 'repair_lifecycle_contract')])
def test_unblocked_status_actions_without_dependencies(status, action):
    row = project({'task': {'id': 'r', 'status': status, 'assignee': 'default'}, 'deadline_at': 2000}, now=1)
    assert row['action'] == action
    if status == 'todo':
        assert row['violations'] == ['dependency_transition_due']
        assert row['blocker']['prerequisite'] == 'Existing recompute_ready transition'
    else:
        assert row['blocker'] is None and row['evidence_state'] == 'unknown'


def test_dependency_prerequisite_names_status_or_unknown():
    deps = [{'task_id': 'p1', 'status': 'blocked', 'trigger': 'a'}, {'task_id': 'p2', 'status': None, 'trigger': 'b'}]
    row = project({'task': {'id': 'r', 'status': 'todo', 'assignee': 'default'},
                   'deadline_at': 2000, 'dependencies': deps}, now=1)
    assert row['blocker']['prerequisite'] == 'p1 (blocked); p2 (unknown)'
    assert row['blocker']['trigger'] == 'a; b'


def test_text_report_trigger_and_exact_violations(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace
    from hermes_cli import kanban_actionability_cli as cli
    base = {'status': 'triage', 'owner': {'kind': 'assignee', 'refs': ['a']}, 'action': 'decide_scope',
            'escalation': {'at': 5}, 'evidence_state': 'known'}
    rows = [{**base, 'task_id': 'A', 'blocker': {'trigger': 'TRIG'}, 'instruction': 'INSTR', 'violations': ['v1', 'v2']},
            {**base, 'task_id': 'B', 'blocker': None, 'instruction': 'PLAIN', 'violations': []}]
    calls = []
    def report(*args, **kwargs):
        calls.append(kwargs)
        return {'total_nonterminal': 2, 'known_violation_count': 1, 'unknown_count': 0,
                'rows': rows, 'has_more': False, 'next_after': None}
    monkeypatch.setattr(cli, 'report', report)
    from hermes_cli import kanban_db as kb
    db_path = tmp_path / 'report.db'
    db_path.touch()
    monkeypatch.setattr(kb, 'kanban_db_path', lambda: db_path)
    assert cli.command(SimpleNamespace(limit=10, after=None, json=False)) == 0
    a, b = [line for line in capsys.readouterr().out.splitlines() if line[:2] in ('A ', 'B ')]
    assert ': TRIG;' in a and 'INSTR' not in a and a.endswith('violations=v1,v2')
    assert ': PLAIN;' in b and b.endswith('violations=none observed')
    assert calls[0]['after'] == ''


def test_fractional_nested_deadline_is_not_lost_by_truthy_fallback():
    conn = complete_fixture()
    event(conn, 's', 'spend_budget_triaged', {'cause': 'scope_stopped'}, 100)
    event(conn, 'n', 'decision_retained', {'episode_uid': 's', 'next_action': {'escalation': {'at': 0.5}}}, 101)
    assert describe(conn, 'root', now=200)['escalation']['at'] == 0
    conn.close()


def test_only_stop_without_episode_adds_stop_identity_uncertainty(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = complete_fixture()
    seen = {}
    monkeypatch.setattr(sources, 'project', lambda facts, now: seen.update(facts) or project(facts, now=now))
    sources.describe(conn, 'root', now=200)
    assert seen['stop'] is None and seen['stop_episode'] is None and seen['unknown'] is False
    conn.close()


def test_reader_schema_cache_avoids_repeated_pragma_queries():
    conn = complete_fixture()
    reader = _reader(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    assert reader.columns('tasks') == reader.columns('tasks')
    assert sum('pragma_table_info' in s and 'tasks' in s for s in statements) == 1
    conn.close()


@pytest.mark.parametrize('entry', ['describe', '_describe'])
def test_provided_reader_is_reused_without_rebuilding_schema(monkeypatch, entry):
    from hermes_cli import kanban_actionability_sources as sources
    conn = complete_fixture()
    reader = _reader(conn)
    def unexpected(*args):
        raise AssertionError('provided Reader must be reused')
    monkeypatch.setattr(sources, 'Reader', unexpected)
    assert getattr(sources, entry)(conn, 'root', reader=reader, now=200)['task_id'] == 'root'
    conn.close()


def test_private_describe_defensive_defaults_and_missing_task(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = held_fixture()
    monkeypatch.setattr(sources.time, 'time', lambda: 400)
    assert sources._describe(conn, 'root', now=None, reader=None)['escalation']['at'] == 500
    with pytest.raises(ValueError, match='unknown task: ghost'):
        sources._describe(conn, 'ghost', now=200, reader=None)
    conn.close()


def test_no_current_run_id_does_not_issue_null_run_query():
    conn = complete_fixture()
    statements = []
    conn.set_trace_callback(statements.append)
    describe(conn, 'root', now=200)
    assert not any('FROM task_runs' in s for s in statements)
    conn.close()


def test_synced_dependency_unknown_is_visible_even_when_parent_satisfied():
    from hermes_cli.kanban_actionability_sources import _dependencies
    conn = complete_fixture()
    conn.executescript("CREATE TABLE kanban_sync_state(mode TEXT); INSERT INTO kanban_sync_state VALUES('primary');")
    conn.execute("UPDATE tasks SET status='done' WHERE id='owner'")
    conn.execute("INSERT INTO task_links VALUES('owner','root')")
    assert _dependencies(_reader(conn), 'root') == ([], True)
    conn.close()


def test_carrier_restriction_remains_unavailable_without_stop_row(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = complete_fixture()
    # Isolate independent restriction annotation; never infer release from absent stop.
    monkeypatch.setattr(sources, '_state', lambda *args: (None, True, False))
    assert sources._route_owner(_reader(conn), {'task_id': 'owner'}) == {'kind': 'unavailable', 'refs': ['owner']}
    conn.close()


def test_uid_rejects_foreign_keys_outside_observed_schema():
    conn = complete_fixture()
    reader = _reader(conn)
    reader.event_columns.discard('uid')
    assert reader.uid({'id': 7, 'uid': 'not-in-observed-schema'}) == '7'
    conn.close()


@pytest.mark.parametrize('damage', ['empty_uid', 'missing_uid', 'partial_state', 'synced_state'])
def test_private_fact_uncertainty_guards_remain_independent(monkeypatch, damage):
    from hermes_cli import kanban_actionability_sources as sources
    conn = held_fixture()
    reader = _reader(conn)
    seen = {}
    monkeypatch.setattr(sources, 'project', lambda facts, now: seen.update(facts) or project(facts, now=now))
    if damage in ('empty_uid', 'missing_uid'):
        monkeypatch.setattr(reader, 'uid', lambda event: '' if damage == 'empty_uid' else None)
        # Keep only a known ordinary material source; stop-identity uncertainty must not mask uid evidence.
        monkeypatch.setattr(sources, '_state', lambda *args: (None, False, False))
    if damage == 'partial_state':
        reader.partial = True
        monkeypatch.setattr(sources, '_state', lambda *args: (None, False, False))
    if damage == 'synced_state':
        reader.synced = True
        monkeypatch.setattr(sources, '_state', lambda *args: (None, False, False))
    sources.describe(conn, 'root', now=200, reader=reader)
    assert seen['unknown'] is True
    conn.close()


def test_empty_uid_stop_normalizes_episode_at_injected_reader_boundary(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = held_fixture()
    reader = _reader(conn)
    monkeypatch.setattr(reader, 'uid', lambda event: '')
    seen = {}
    monkeypatch.setattr(sources, 'project', lambda facts, now: seen.update(facts) or project(facts, now=now))
    sources.describe(conn, 'root', now=200, reader=reader)
    assert seen['stop_episode'] is None
    conn.close()


def test_dependency_blank_step_from_private_seam_uses_safe_trigger(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = complete_fixture()
    conn.execute("INSERT INTO task_links VALUES('owner','root')")
    monkeypatch.setattr(sources, '_human_step', lambda request: ' ')
    deps, _ = sources._dependencies(_reader(conn), 'root')
    assert deps[0]['trigger'] == 'owner: accountable owner completes its acceptance'
    conn.close()


def test_unscoped_contract_cannot_supply_none_episode_administration():
    from hermes_cli.kanban_actionability_sources import _administration
    conn = complete_fixture()
    event(conn, 'unscoped', 'decision_retained', {'next_action': {'escalate_at': 900}}, 100)
    reader = _reader(conn)
    assert _administration(reader, reader.events('root'), None)[1] == {'escalate_at': None}
    conn.close()


def test_public_default_time_is_normalized_before_private_boundary(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = complete_fixture()
    seen = {}
    monkeypatch.setattr(sources.time, 'time', lambda: 777)
    monkeypatch.setattr(sources, '_describe', lambda conn, tid, **kwargs: seen.update(kwargs) or {})
    sources.describe(conn, 'root')
    assert seen['now'] == 777
    conn.close()


def test_material_source_uid_is_carried_into_project_identity(monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    conn = complete_fixture()
    event(conn, 'material', 'blocked', {}, 100)
    seen = {}
    monkeypatch.setattr(sources, 'project', lambda facts, now: seen.update(facts) or project(facts, now=now))
    sources.describe(conn, 'root', now=200)
    assert seen['source_uid'] == 'material'
    conn.close()


def test_missing_command_table_returns_before_schema_lookup(monkeypatch):
    conn = complete_fixture()
    reader = _reader(conn)
    reader.tables.discard('kanban_failure_commands')
    def unexpected(table):
        raise AssertionError('missing optional table cannot require a schema lookup')
    monkeypatch.setattr(reader, 'columns', unexpected)
    assert reader.pending('root') is None
    conn.close()
