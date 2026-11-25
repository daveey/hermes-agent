"""Single-fault contract cases for every clause of kanban_history_residue_source.

Each malformed input isolates a predicate while keeping earlier checks satisfied
(including the frozen wire echo). Most edits admit malformed input or allow a
non-Refused exception to escape. C318 instead pins the diagnostic reason at the
proof-object boundary; that is error-contract coverage, not an authorization kill.
True redundant clauses are documented separately, never reported as killed.
"""
import copy
import json

import pytest

from hermes_cli import kanban_history_residue_source as source

from tests.hermes_cli.test_kanban_history_residue_source import specimen

CONTAINMENT = source.CONTAINMENT
SPEND = source.SPEND
KEYS = ('uid', 'id', 'version', 'created_on', 'status', 'current_run_uid', 'lease_holder',
        'lease_run_uid', 'lease_expires', 'deleted_at', 'delete_pending')


def build(row_updates=None, fields_updates=None, payload=None):
    """A self-consistent source row: the wire echo always matches whatever fields say."""
    row = specimen()
    fields = json.loads(row['fields'])
    fields.update(fields_updates or {})
    if payload is not None:
        fields['payload'] = json.dumps(payload)
    row['fields'] = json.dumps(fields)
    row.update(row_updates or {})
    wire = {'client_op_id': row['op_id'], 'table': row['table_name'], 'uid': row['row_uid'],
            'op': row['op'], 'fields': fields, 'hlc': row['hlc'], 'frozen_replay': 1}
    row['wire_json'] = json.dumps(wire)
    return row


def refused(row, reason):
    with pytest.raises(source.Refused, match='^' + reason + '$'):
        source.decode_source(json.dumps(row))


def blocked(**payload):
    base = {'metadata_only': True, 'classified_in_place': True}
    base.update(payload)
    return build({'last_error': CONTAINMENT}, {'kind': 'blocked'}, base)


def proof():
    raw = json.dumps(specimen())
    parent = {'uid': 't_stopped', 'id': 't_stopped', 'version': 7, 'created_on': 'home', 'status': 'triage',
              'current_run_uid': None, 'lease_holder': None, 'lease_run_uid': None,
              'lease_expires': None, 'deleted_at': None, 'delete_pending': 0}
    absence = {'board': 'default', 'table': 'task_events', 'uid': 'event-original',
               'state': 'never_existed', 'floor': 0, 'head': 100, 'row': None, 'tombstone': None}
    receipt = {'version': 1, 'gateway': 'home', 'source_json': raw, 'reason': 'Keep history',
               'disposition': 'retained_unreplayed', 'canonical_acceptance': False,
               'historical_identity_certified': False, 'execution_authority': False,
               'reported_rejection_only': True, 'snapshot_only': True, 'observed_at_ms': 50,
               'observed_head': 100, 'parent': copy.deepcopy(parent), 'absence': copy.deepcopy(absence)}
    current = {'observed_at_ms': 90, 'observed_head': 100, 'parent': copy.deepcopy(parent),
               'absence': copy.deepcopy(absence), 'no_lease': True, 'no_running_run': True,
               'no_retained_op_receipt': True, 'op_history_certified': False,
               'retained_receipt_floor1': None, 'execution_authority': False}
    return raw, {'version': 1, 'gateway': 'home', 'receipt': receipt, 'current': current}


def receipt_refused(mutate, reason):
    raw, data = proof()
    receipt = data['receipt']
    mutate(receipt)
    with pytest.raises(source.Refused, match='^' + reason + '$'):
        source.validate_receipt(receipt, raw, gateway='home', board='default')


def current_refused(mutate, reason, head=100):
    raw, data = proof()
    mutate(data)
    with pytest.raises(source.Refused, match='^' + reason + '$'):
        source.validate_current(json.dumps(data), raw, gateway='home', board='default', head=head)


# --- baselines: the single-fault inputs below differ from these in exactly one predicate ---

def test_baselines_are_accepted():
    for row in (build(), blocked(), build({'op_id': 'x' * 2048}), build({'hlc': 'h' * 2048})):
        assert source.decode_source(json.dumps(row)) == row
    raw, data = proof()
    source.validate_receipt(data['receipt'], raw, gateway='home', board='default')
    assert source.validate_current(json.dumps(data), raw, gateway='home', board='default', head=100) == data['current']


# C243: not isinstance(raw, str) -> False (non-str would reach raw.encode and raise AttributeError)
@pytest.mark.parametrize('raw', [5, None, b'{}', {}, []])
def test_c243_non_text_json_input_is_a_refusal_not_an_attribute_error(raw):
    with pytest.raises(source.Refused, match='^json_required$'):
        source.strict_object(raw)
    with pytest.raises(source.Refused, match='^json_required$'):
        source.decode_source(raw)


@pytest.mark.parametrize('layer', ['fields', 'wire_json'])
def test_c243_nested_layer_must_be_embedded_json_text(layer):
    row = specimen()
    row[layer] = json.loads(row[layer])
    with pytest.raises(source.Refused, match='^json_required$'):
        source.decode_source(json.dumps(row))


# C254 (isinstance str), C256 (len <= 2048), C273/C274/C275 (_text of op_id/row_uid/hlc)
@pytest.mark.parametrize('key', ['op_id', 'row_uid', 'hlc'])
@pytest.mark.parametrize('bad', [5, None, True, [], '', '   '])
def test_c254_c273_c274_c275_identity_texts_must_be_nonblank_strings(key, bad):
    refused(build({key: bad}), 'source_shape')


@pytest.mark.parametrize('key', ['op_id', 'row_uid', 'hlc'])
def test_c256_identity_texts_are_length_bounded_on_a_consistent_wire(key):
    refused(build({key: 'x' * 2049}), 'source_shape')


@pytest.mark.parametrize('bad', ['x' * 2049])
def test_c256_task_id_text_is_length_bounded(bad):
    refused(build(fields_updates={'task_id': bad}), 'source_shape')


@pytest.mark.parametrize('bad', [5, None, 'x' * 2049])
def test_c254_c256_receipt_reason_is_text(bad):
    receipt_refused(lambda r: r.update(reason=bad), 'receipt_identity')


# C276 / C277: table and op literals, wire echo kept consistent so only the shape check can refuse
@pytest.mark.parametrize('value', ['tasks', '', None])
def test_c276_table_name_must_be_task_events(value):
    refused(build({'table_name': value}), 'source_shape')


@pytest.mark.parametrize('value', ['delete', '', None])
def test_c277_op_must_be_upsert(value):
    refused(build({'op': value}), 'source_shape')


# C259 / C260 / C261 / C262: each clause of the containment branch alone
@pytest.mark.parametrize('last_error', [SPEND, 'unknown', None, ''])
def test_c259_containment_branch_requires_the_containment_error(last_error):
    row = blocked()
    row['last_error'] = last_error
    refused(row, 'unsupported_rejection')


@pytest.mark.parametrize('kind', ['promoted_manual', 'started', 'Blocked'])
def test_c260_containment_error_requires_blocked_kind(kind):
    row = build({'last_error': CONTAINMENT}, {'kind': kind},
                {'metadata_only': True, 'classified_in_place': True})
    refused(row, 'unsupported_rejection')


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_c261_metadata_only_must_be_literal_true(value):
    refused(blocked(metadata_only=value), 'unsupported_rejection')


def test_c261_metadata_only_may_not_be_absent():
    row = build({'last_error': CONTAINMENT}, {'kind': 'blocked'}, {'classified_in_place': True})
    refused(row, 'unsupported_rejection')


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_c262_classified_in_place_must_be_literal_true(value):
    refused(blocked(classified_in_place=value), 'unsupported_rejection')


def test_c262_classified_in_place_may_not_be_absent():
    row = build({'last_error': CONTAINMENT}, {'kind': 'blocked'}, {'metadata_only': True})
    refused(row, 'unsupported_rejection')


# --- _absence ---

def set_receipt_abs(**kw):
    return lambda r: r['absence'].update(kw)


def set_current_abs(**kw):
    return lambda d: d['current']['absence'].update(kw)


# C247 / C249 / C300 / C311 / C318 / C319: structural type guards precede attribute access
@pytest.mark.parametrize('bad', [None, [], 'x', 5])
def test_c247_receipt_absence_must_be_object(bad):
    receipt_refused(lambda r: r.update(absence=bad), 'absence_object')


@pytest.mark.parametrize('bad', [None, [], 'x', 5])
def test_c247_current_absence_must_be_object(bad):
    current_refused(lambda d: d['current'].update(absence=bad), 'absence_object')


@pytest.mark.parametrize('bad', [None, [], 'x', 5])
def test_c249_current_parent_must_be_object(bad):
    current_refused(lambda d: d['current'].update(parent=bad), 'current_parent')


@pytest.mark.parametrize('kind', ['none', 'list', 'keys-as-list', 'str'])
def test_c300_receipt_must_be_object_even_if_its_iterable_names_match(kind):
    raw, data = proof()
    keys = list(data['receipt'])
    bad = {'none': None, 'list': [], 'keys-as-list': keys, 'str': 'x'}[kind]
    with pytest.raises(source.Refused, match='^receipt_keys$'):
        source.validate_receipt(bad, raw, gateway='home', board='default')


@pytest.mark.parametrize('bad', [None, [], 'x', 5])
def test_c311_receipt_parent_must_be_object(bad):
    receipt_refused(lambda r: r.update(parent=bad), 'historical_snapshot')


@pytest.mark.parametrize('kind', ['none', 'list', 'keys-as-list'])
def test_c318_proof_receipt_must_be_object_at_the_response_guard(kind):
    # Without the guard validate_receipt's own check would refuse with 'receipt_keys' instead.
    bad = {'none': None, 'list': [], 'keys-as-list': list(proof()[1]['receipt'])}[kind]
    current_refused(lambda d: d.update(receipt=bad), 'proof_objects')


@pytest.mark.parametrize('kind', ['none', 'list', 'keys-as-list'])
def test_c319_proof_current_must_be_object_at_the_response_guard(kind):
    # set(current) of a key list equals the key set, so only the guard stops it before .get.
    bad = {'none': None, 'list': [], 'keys-as-list': list(proof()[1]['current'])}[kind]
    current_refused(lambda d: d.update(current=bad), 'proof_objects')


# C291 / C292 / C294 / C296 / C298: absence record clauses, one fault each
@pytest.mark.parametrize('head', [100.0, True, '100', None])
def test_c291_receipt_absence_head_must_be_int(head):
    receipt_refused(set_receipt_abs(head=head), 'absence_unproven')


@pytest.mark.parametrize('head', [100.0, '100', None])
def test_c291_current_absence_head_must_be_int(head):
    current_refused(set_current_abs(head=head), 'absence_unproven')


def test_c291_bool_head_cannot_equal_integer_cut_head():
    raw, data = proof()
    for section in ('receipt', 'current'):
        data[section]['observed_head'] = 1
        data[section]['absence']['head'] = True
    with pytest.raises(source.Refused, match='^absence_unproven$'):
        source.validate_receipt(data['receipt'], raw, gateway='home', board='default')
    data['receipt']['absence']['head'] = 1
    with pytest.raises(source.Refused, match='^absence_unproven$'):
        source.validate_current(json.dumps(data), raw, gateway='home', board='default', head=1)


@pytest.mark.parametrize('head', [99, 101, 0])
def test_c292_absence_head_must_equal_the_cut_head(head):
    receipt_refused(set_receipt_abs(head=head), 'absence_unproven')
    current_refused(set_current_abs(head=head), 'absence_unproven')


@pytest.mark.parametrize('floor', [False, 0.0, '0', None])
def test_c294_absence_floor_must_be_int_zero(floor):
    receipt_refused(set_receipt_abs(floor=floor), 'absence_unproven')
    current_refused(set_current_abs(floor=floor), 'absence_unproven')


@pytest.mark.parametrize('key', ['row', 'tombstone'])
def test_c296_c298_absence_must_state_null_row_and_tombstone(key):
    def drop(section):
        return lambda x: (x if section == 'receipt' else x['current'])['absence'].pop(key)
    receipt_refused(drop('receipt'), 'absence_unproven')
    current_refused(drop('current'), 'absence_unproven')


# --- validate_receipt ---

@pytest.mark.parametrize('version', [True, 1.0, '1'])
def test_c302_receipt_version_must_be_int(version):
    receipt_refused(lambda r: r.update(version=version), 'receipt_identity')


@pytest.mark.parametrize('version', [0, 2, -1])
def test_c303_receipt_version_must_be_one(version):
    receipt_refused(lambda r: r.update(version=version), 'receipt_identity')


@pytest.mark.parametrize('value', ['retained_replayed', '', None])
def test_c306_disposition_must_be_retained_unreplayed(value):
    receipt_refused(lambda r: r.update(disposition=value), 'receipt_identity')


@pytest.mark.parametrize('value', ['', '   ', None, 5])
def test_c307_receipt_reason_must_be_text(value):
    receipt_refused(lambda r: r.update(reason=value), 'receipt_identity')


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '50', None, 2**53])
def test_c308_receipt_observed_at_must_be_positive_int(value):
    receipt_refused(lambda r: r.update(observed_at_ms=value), 'historical_snapshot')


@pytest.mark.parametrize('head', [100.0, True])
def test_c309_receipt_observed_head_must_be_int(head):
    def mutate(r):
        r['observed_head'] = head
        r['absence']['head'] = 1 if head is True else 100
    receipt_refused(mutate, 'historical_snapshot')


def test_c310_receipt_observed_head_is_not_negative():
    def mutate(r):
        r['observed_head'] = -1
        r['absence']['head'] = -1
    receipt_refused(mutate, 'historical_snapshot')


@pytest.mark.parametrize('key,value', [('created_on', 'other'), ('created_on', None), ('uid', 'other')])
def test_c313_receipt_parent_is_bound_to_task_and_gateway(key, value):
    receipt_refused(lambda r: r['parent'].update({key: value}), 'historical_snapshot')


# --- validate_current ---

@pytest.mark.parametrize('extra', [{'extra': 1}, {'op_unknown': True}])
def test_c314_response_keys_are_exact(extra):
    current_refused(lambda d: d.update(extra), 'proof_identity')


def test_c314_missing_response_key_is_refused():
    current_refused(lambda d: d.pop('current'), 'proof_identity')


@pytest.mark.parametrize('version', [0, 2])
def test_c316_response_version_must_be_one(version):
    current_refused(lambda d: d.update(version=version), 'proof_identity')


@pytest.mark.parametrize('head', [100.0, True])
def test_c321_cut_head_argument_must_be_int(head):
    def mutate(d):
        if head is True:  # every recorded head is 1 so only the argument's type is wrong
            for section in ('receipt', 'current'):
                d[section]['observed_head'] = 1
                d[section]['absence']['head'] = 1
    current_refused(mutate, 'current_cut', head=head)  # type: ignore[arg-type]


@pytest.mark.parametrize('observed', [100.0, True])
def test_c323_current_observed_head_must_be_int(observed):
    def mutate(d):
        d['current']['observed_head'] = observed
        if observed is True:
            d['current']['absence']['head'] = 1
    current_refused(mutate, 'current_cut', head=1 if observed is True else 100)


def test_c322_cut_head_is_not_negative():
    def mutate(d):
        d['current']['observed_head'] = -1
        d['current']['absence']['head'] = -1
    current_refused(mutate, 'current_cut', head=-1)


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '90', None, 2**53])
def test_c325_current_observed_at_must_be_positive_int(value):
    current_refused(lambda d: d['current'].update(observed_at_ms=value), 'current_cut')


def test_c326_missing_retention_key_is_refused_by_the_exact_key_set():
    # Equivalence evidence for C326 (not a kill): the guard at line 152 already forces the key.
    current_refused(lambda d: d['current'].pop('retained_receipt_floor1'), 'current_keys')


@pytest.mark.parametrize('value', [5, True, {}, [], 1.5])
def test_c327_retention_metadata_is_null_or_text(value):
    current_refused(lambda d: d['current'].update(retained_receipt_floor1=value), 'receipt_retention_metadata')


@pytest.mark.parametrize('value', ['other', None, 7, 't_stopped '])
def test_c329_parent_id_must_equal_task_id(value):
    current_refused(lambda d: d['current']['parent'].update(id=value), 'current_parent')


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '7', None, 2**53])
def test_c331_parent_version_must_be_positive_int(value):
    current_refused(lambda d: d['current']['parent'].update(version=value), 'current_parent')


@pytest.mark.parametrize('key', ['current_run_uid', 'lease_holder', 'lease_run_uid', 'lease_expires', 'deleted_at'])
def test_c382_active_pointer_keys_must_be_present(key):
    current_refused(lambda d: d['current']['parent'].pop(key), 'current_parent_active')


@pytest.mark.parametrize('key', ['current_run_uid', 'lease_holder', 'lease_run_uid', 'lease_expires', 'deleted_at'])
@pytest.mark.parametrize('value', [0, '', False])
def test_active_pointer_keys_must_be_null_not_falsy(key, value):
    current_refused(lambda d: d['current']['parent'].update({key: value}), 'current_parent_active')
