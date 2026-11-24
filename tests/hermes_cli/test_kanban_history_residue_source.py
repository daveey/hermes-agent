"""Exact-byte history-residue parsing is not retirement or execution authority."""
import json

import pytest

from hermes_cli import kanban_history_residue_source as source


def specimen():
    fields = {'task_id': 't_stopped', 'run_uid': None, 'kind': 'promoted_manual',
              'payload': '{}', 'created_at': 100}
    wire = {'client_op_id': 'op-original', 'table': 'task_events', 'uid': 'event-original',
            'op': 'upsert', 'fields': fields, 'hlc': '100-0000-home', 'frozen_replay': 1}
    return {'op_id': wire['client_op_id'], 'table_name': wire['table'], 'row_uid': wire['uid'],
            'op': wire['op'], 'fields': json.dumps(fields), 'wire_json': json.dumps(wire),
            'hlc': wire['hlc'], 'state': 'rejected', 'attempts': 2, 'identity_state': 'ambiguous',
            'last_error': 'spend_decision_pending: restricted episode has no execution release'}


def test_preserves_exact_source_and_unverified_ambiguous_claim():
    row = specimen()
    raw = json.dumps(row, indent=2)
    assert source.decode_source(raw) == row
    assert row['identity_state'] == 'ambiguous'


@pytest.mark.parametrize('layer', ['source', 'fields', 'wire_json', 'payload'])
def test_duplicate_keys_refused_before_information_is_lost(layer):
    row = specimen()
    if layer == 'source':
        raw = json.dumps(row)[:-1] + ',"state":"rejected"}'
    else:
        if layer == 'payload':
            fields = json.loads(row['fields'])
            fields['payload'] = '{"same":1,"same":1}'
            row['fields'] = json.dumps(fields)
            wire = json.loads(row['wire_json'])
            wire['fields'] = fields
            row['wire_json'] = json.dumps(wire)
        else:
            key = 'task_id' if layer == 'fields' else 'client_op_id'
            value = 't_stopped' if layer == 'fields' else 'op-original'
            row[layer] = row[layer][:-1] + ',' + json.dumps(key) + ':' + json.dumps(value) + '}'
        raw = json.dumps(row)
    with pytest.raises(source.Refused, match='^duplicate_key$'):
        source.decode_source(raw)


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-Infinity', '1e999'])
def test_nonfinite_json_is_never_canonical_evidence(bad):
    row = specimen()
    raw = json.dumps(row)[:-1] + ',"unverified":' + bad + '}'
    with pytest.raises(source.Refused, match='^nonfinite_number$'):
        source.decode_source(raw)


@pytest.mark.parametrize('layer', ['source', 'fields', 'wire_json', 'payload'])
def test_malformed_unicode_and_excess_depth_refused(layer):
    row = specimen()
    if layer == 'source':
        row['unverified'] = '\ud800'
    elif layer == 'payload':
        fields = json.loads(row['fields'])
        fields['payload'] = '{"bad":"\\ud800"}'
        row['fields'] = json.dumps(fields)
    else:
        row[layer] = '{"bad":"\\ud800"}'
    with pytest.raises(source.Refused, match='^invalid_unicode$'):
        source.decode_source(json.dumps(row))
    row = specimen()
    raw = json.dumps(row)[:-1] + ',"deep":' + '[' * 80 + '0' + ']' * 80 + '}'
    with pytest.raises(source.Refused, match='^json_depth$'):
        source.decode_source(raw)


@pytest.mark.parametrize('key,value', [('attempts', True), ('attempts', 0), ('hlc', ''),
    ('op_id', ''), ('row_uid', ''), ('table_name', 'tasks'), ('op', 'delete'),
    ('state', 'pending'), ('last_error', 'unknown')])
def test_unsupported_or_inconsistent_source_is_refused(key, value):
    row = specimen()
    row[key] = value
    with pytest.raises(source.Refused):
        source.decode_source(json.dumps(row))


@pytest.mark.parametrize('key,value', [('run_uid', 'run'), ('created_at', True),
    ('created_at', 0), ('created_at', 2**53), ('kind', 'started'), ('task_id', '')])
def test_adjacent_valid_frozen_field_shapes_are_refused(key, value):
    row = specimen()
    fields = json.loads(row['fields'])
    fields[key] = value
    row['fields'] = json.dumps(fields)
    wire = json.loads(row['wire_json'])
    wire['fields'] = fields
    row['wire_json'] = json.dumps(wire)
    with pytest.raises(source.Refused):
        source.decode_source(json.dumps(row))


def test_blocked_metadata_only_source_supported_without_certifying_it():
    row = specimen()
    fields = json.loads(row['fields'])
    fields.update(kind='blocked', payload='{"metadata_only":true,"classified_in_place":true}')
    row['fields'] = json.dumps(fields)
    wire = json.loads(row['wire_json'])
    wire['fields'] = fields
    row['wire_json'] = json.dumps(wire)
    row['last_error'] = 'containment_hold: use scoped resume-contained; generic writes cannot satisfy containment'
    assert source.decode_source(json.dumps(row)) == row


def test_bool_frozen_flag_cannot_equal_integer_flag():
    row = specimen()
    wire = json.loads(row['wire_json'])
    wire['frozen_replay'] = True
    row['wire_json'] = json.dumps(wire)
    with pytest.raises(source.Refused, match='^frozen_wire_mismatch$'):
        source.decode_source(json.dumps(row))
