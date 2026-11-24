"""Schema census and real canonical sender -> owner storage parity.

Every model-facing property needs an explicit fixture/decision here. The test
courier parses the real argv; it never substitutes the sender's in-memory args.
The older field/landing suites remain independent regression tests, unchanged.
"""
import copy
import json
import os

import pytest

from hermes_cli import kanban_creation_home as creation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement as placement
from tests.tools import test_kanban_worker_canonical_home as home
from tests.tools.test_kanban_home_landing_contract import REQUEST
from tools import kanban_tools as tools
from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA

svc, gws = home.svc, home.gws


@pytest.fixture
def pool_labels(monkeypatch, gws):
    # Do not make DB setup autouse: the independent wire census is pure/fast.
    return home.pool_labels.__wrapped__(monkeypatch, gws)
PROPERTIES = KANBAN_CREATE_SCHEMA['parameters']['properties']
MISSING = object()
# Deliberately independent of production's exclusion table: adding an exclusion
# must get a reviewable policy decision, not silently remove a regression case.
EXCLUDED = {
    'board': 'routing envelope; only the default board is supported',
    'placement': 'consumed into execution runs_on, never a storage-home pin',
    'workspace_kind': 'host-local source; only scratch is portable',
    'workspace_path': 'host-local source cannot be transferred by the courier',
    'project': 'host-local project source cannot be transferred by the courier',
    'origin_session': 'qualified transport identity, not a receiver-local argument',
    'force_hub': 'operator override must never be forwarded',
}
# Empty means an explicitly supplied false/zero/empty-container, not omission.
# Some empty values are invalid: identical typed refusal is part of the contract.
FIXTURES = {
    'title': ('', 'Schema contract Ω = exact title'),
    'assignee': ('', 'default'),
    'requires': ([], ['terminal']),
    'body': ('', 'Canonical bytes Ω\n\nline = value\n  trailing spaces  \n'),
    'parents': ([], ['PARENT']),
    'tenant': ('', 'tenant-explicit'),
    'priority': (0, 17),
    'admission_class': ('', 'normal'),
    'admission_frees': ('', 'frees: one fixture capacity slot'),
    'self_work': ({}, {'version': 1, 'repository': 'hermes-core', 'area': 'kanban', 'kind': 'test-gap'}),
    'workspace_kind': ('', 'scratch'),
    'workspace_path': ('', '/nonportable/schema-contract-source'),
    'project': ('', 'schema-contract-missing-project'),
    'triage': (False, True),
    'distinct_from': ([], ['PARENT']),
    'distinct_reason': ('', 'Separate fixture acceptance criteria, not the predecessor scope.'),
    'hub_reason': ('', 'Fixture rationale retained verbatim, not appended to the body.'),
    'force_hub': (False, True),
    'idempotency_key': ('', 'schema-contract-key'),
    'origin_session': ('', '@session:default@worker-1/canonical-home-owner'),
    'runs_on': ([], ['machine:macbook']),
    'placement': ('', 'macbook'),
    'max_runtime_seconds': (0, 120),
    'max_retries': (0, 2),
    'human_request': ({}, REQUEST),
    'initial_status': ('', 'blocked'),
    'human_action_now': (False, True),
    'skills': ([], ['five-whys']),
    'review_required': (False, True),
    'goal_mode': (False, True),
    'completion_contract': ('', 'nousresearch/hermes-agent'),
    'goal_max_turns': (0, 7),
    'model': ('', 'class:deep'),
    'provider': ('', 'auto'),
    'pin_mandatory': (False, True),
    'board': ('', 'default'),
}


def test_schema_has_explicit_fixture_and_transport_decision():
    assert set(FIXTURES) == set(PROPERTIES), 'Schema changed: add omitted/empty/set fixtures and a transport decision'
    from hermes_cli import kanban_creation_contract as contract
    assert set(contract.EXCLUDED) & set(PROPERTIES) == set(EXCLUDED)
    for field, reason in contract.EXCLUDED.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 15, (field, reason)
    # Internal compatibility inputs also need documented decisions.
    for field in ('project_id', 'session_id'):
        assert field in contract.EXCLUDED


@pytest.mark.parametrize('field', sorted(PROPERTIES))
@pytest.mark.parametrize('shape', ['omitted', 'empty', 'set'])
def test_schema_transport_options_are_full_minus_documented_exclusions(monkeypatch, field, shape):
    monkeypatch.setattr(placement, 'hub_runner', lambda argv: (
        0, '--option --canonical-home canonical-home-v2 canonical-home-v1', ''))
    args = {'title': 'Transport census', 'requires': [], 'runs_on': []}
    args.pop(field, None)
    if shape != 'omitted':
        args[field] = copy.deepcopy(FIXTURES[field][shape == 'set'])
    options = creation.transport_options(args, hub_cli='fixture-courier')
    expected = {key: value for key, value in args.items() if key not in EXCLUDED and value is not None}
    assert options == expected


@pytest.mark.parametrize('help_text', ['--option canonical-home-v1', '--canonical-home canonical-home-v2', ''])
def test_canonical_contract_refuses_unnegotiated_client(monkeypatch, help_text):
    monkeypatch.setattr(placement, 'hub_runner', lambda argv: (0, help_text, ''))
    with pytest.raises(ValueError, match='canonical_home_transport_unavailable'):
        creation.transport_options({'title': 'No downgrade', 'requires': [], 'runs_on': []},
                                   hub_cli='fixture-courier')


def test_transport_omits_none_not_explicit_false_or_empty(monkeypatch):
    monkeypatch.setattr(placement, 'hub_runner', lambda argv: (
        0, '--option --canonical-home canonical-home-v2 canonical-home-v1', ''))
    options = creation.transport_options({'title': 'Default semantics', 'requires': [],
                                         'skills': None, 'priority': 0, 'review_required': False},
                                        hub_cli='fixture-courier')
    assert options == {'title': 'Default semantics', 'requires': [], 'priority': 0,
                       'review_required': False}


def _options(argv):
    result = {}
    for index, token in enumerate(argv):
        if token != '--option':
            continue
        key, raw = argv[index + 1].split('=', 1)
        assert key not in result, f'duplicate --option {key}'
        result[key] = json.loads(raw)
    return result


# These are identity/audit records, not execution fields. Every removed column is
# named explicitly; newly introduced storage columns stay in the parity check.
NONDETERMINISTIC = {'id', 'created_at', 'updated_at', 'status_changed_at', 'ready_at',
                    'started_at', 'completed_at', 'last_activity_at'}
PROVENANCE = {'created_on', 'session_id', 'created_by', 'creator_task_id',
              'native_origin', 'chat_origin', 'creation_receipt', 'placement_audit',
              'placement_policy', 'origin_intent', 'origin_request_id'}


def _stored(gateway, task_id):
    row = dict(gateway.conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
    return {key: value for key, value in row.items() if key not in NONDETERMINISTIC | PROVENANCE}


def _refusal(result):
    assert not result.get('task_id'), result
    assert result.get('error'), result
    # Not every native handler error has a code; never accept an untyped/empty
    # transport failure as equivalent to a receiver validation error.
    return result.get('code'), result['error']


def assert_schema_sender_to_stored_owner_parity(gws, monkeypatch, pool_labels, field, shape):
    hub, worker = gws
    for gateway in gws:
        skill = gateway.home / 'skills' / 'five-whys' / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('---\nname: five-whys\ndescription: Fixture skill\n---\nFixture.\n')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_ORIGIN_SESSION', raising=False)
    monkeypatch.setenv('HERMES_TENANT', 'tenant-source')
    monkeypatch.setattr(placement.reg, 'self_gateway',
                        lambda: 'hub' if os.environ['HERMES_HOME'] == str(hub.home) else 'worker-1')
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)
    monkeypatch.setattr(placement, 'remote_state', lambda _: None)
    placement._help_cache.clear()
    value = MISSING if shape == 'omitted' else copy.deepcopy(FIXTURES[field][shape == 'set'])
    args = {'title': 'Schema contract fixture', 'body': 'Canonical body\n  exact  \n',
            'assignee': 'default', 'requires': [], 'runs_on': []}
    args.pop(field, None)
    if value is not MISSING:
        args[field] = value
    # Supply valid companions without masking the property under test.
    if field == 'provider' and shape == 'set':
        args['model'] = 'class:deep'
    if field in ('initial_status', 'human_request', 'human_action_now'):
        if field != 'initial_status':
            args['initial_status'] = 'blocked'
        if field != 'human_request':
            args['human_request'] = copy.deepcopy(REQUEST)
    if field == 'distinct_from' and shape == 'set':
        args['distinct_reason'] = FIXTURES['distinct_reason'][1]
    # Shared parent identity is an actual stored predecessor on both boards.
    if field in ('parents', 'distinct_from') and shape == 'set':
        parent_ids = []
        for gateway in gws:
            gateway.enter()
            parent_ids.append(kb.create_task(gateway.conn, title='Unrelated fixture predecessor',
                                            assignee='default', requires=[]))
        args[field] = [parent_ids[1]]
    else:
        parent_ids = None

    sent, received = [], []

    def runner(argv):
        if argv[-1] == '--help':
            return 0, '--option --canonical-home canonical-home-v2 canonical-home-v1', ''
        assert argv[1:3] == ['create-task', 'hub']
        sent.append(argv)
        options = _options(argv)
        assert not set(EXCLUDED) & set(options), options
        # Legacy positional fields remain transport envelope compatibility only;
        # exact canonical values are the JSON options, which win at the owner.
        incoming = dict(options)  # v2 takes the complete JSON payload, not positional defaults
        if parent_ids and field in incoming:
            incoming[field] = [parent_ids[0] if x == parent_ids[1] else x for x in incoming[field]]
        intent = {'request_id': argv[argv.index('--id') + 1],
                  'owner': {'gateway': 'hub', 'profile': 'default', 'board': 'default'},
                  'origin': {'gateway': 'worker-1', 'profile': 'default',
                             'session': argv[argv.index('--origin-session') + 1]}}
        hub.enter()
        try:
            received.append(json.loads(tools._handle_create(incoming, origin_intent=intent)))
        finally:
            worker.enter()
        return 3, 'durably queued', ''

    monkeypatch.setattr(placement, 'hub_runner', runner)
    worker.enter()
    # Run the ordinary local front door, changing only routing policy.
    with monkeypatch.context() as local:
        local.setattr(creation, 'plan', lambda *a, **kw: None)
        local.setattr(placement, 'config', dict)
        local_args = copy.deepcopy(args)
        if field == 'runs_on' and shape == 'omitted':
            local_args['runs_on'] = ['pool:worker']  # canonical home's documented execution default
        local_result = json.loads(tools._handle_create(local_args))
    local_row = _stored(worker, local_result['task_id']) if local_result.get('task_id') else None
    if local_row is not None:
        # Don't let the local comparison itself become a cross-board overlap.
        worker.conn.execute('UPDATE tasks SET status="archived" WHERE id=?', (local_result['task_id'],))
        worker.conn.commit()
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    worker.enter()
    forwarded = json.loads(tools._handle_create(copy.deepcopy(args)))

    if field in EXCLUDED:
        from hermes_cli import kanban_creation_contract as contract
        assert contract.EXCLUDED[field].strip(), EXCLUDED[field]
        if sent:
            assert field not in _options(sent[0])
            assert received and received[0].get('task_id'), received
        else:
            assert not forwarded.get('task_id'), forwarded
            assert forwarded.get('error'), forwarded
        return
    if local_row is None:
        assert not received or not received[0].get('task_id'), (local_result, received)
        actual = received[0] if received else forwarded
        assert _refusal(actual) == _refusal(local_result)
        return
    assert len(sent) == len(received) == 1, (forwarded, received)
    assert forwarded.get('placed', {}).get('gateway') == 'hub', forwarded
    assert received[0].get('task_id'), received
    options = _options(sent[0])
    if value is not MISSING:
        assert field in options, (field, value, options)
    if field == 'body' and value is not MISSING:
        assert options['body'] == value
    remote_row = _stored(hub, received[0]['task_id'])
    assert remote_row == local_row
    assert '--canonical-home' in sent[0]
    intent = hub.conn.execute('SELECT task_id FROM origin_creation_intents WHERE request_id=?',
                             (sent[0][sent[0].index('--id') + 1],)).fetchone()
    assert intent['task_id'] == received[0]['task_id']
    assert worker.conn.execute('SELECT 1 FROM tasks WHERE id=?', (received[0]['task_id'],)).fetchone() is None
