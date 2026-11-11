"""Projection bounds apply equally to local DB bytes and attested replicas."""
import json
import sqlite3

import pytest

from hermes_cli.kanban_completion_receipt_projection import read_completed, validate_receipt_source
from tests.hermes_cli.test_kanban_remote_completion_receipt import publication


def source_and_receipt():
    task = publication({'owner': {'task_id': 't_one'}})
    return task['completion_receipt_source'], task['completion_receipt']


@pytest.mark.parametrize('field,value', [
    ('event_uid', 'X' * 50000), ('run_uid', ''), ('event_id', -1),
    ('run_id', 0), ('completed_at', 'not-a-time'),
    ('attached_from_workspace', {'https://example.com/review/1': 'X' * 50000}),
])
def test_local_completed_projection_omits_malformed_provenance(field, value):
    source, receipt = source_and_receipt()
    source[field] = value
    metadata = {'completion_receipt': receipt}
    if 'attached_from_workspace' in source:
        metadata['attached_from_workspace'] = source['attached_from_workspace']
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE task_events(id INTEGER, task_id TEXT, kind TEXT, run_id INTEGER,
                                     created_at, uid TEXT, payload TEXT);
            CREATE TABLE task_runs(id INTEGER, task_id TEXT, outcome TEXT, metadata TEXT, uid TEXT);
        ''')
        db.execute('INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (source['event_id'], 't_one', 'completed', source['run_id'], source['completed_at'],
                    source['event_uid'], json.dumps({'completion_receipt_admission': 1})))
        db.execute('INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)',
                   (source['run_id'], 't_one', 'completed', json.dumps(metadata), source['run_uid']))
        assert read_completed(db, 't_one', gateway='owner', board='build') is None


def test_provenance_is_normalized_copy_and_nullable_uids_are_valid():
    source, receipt = source_and_receipt()
    source['event_uid'] = source['run_uid'] = None
    copied = validate_receipt_source(source, receipt, expected_ref='@task:build@owner/t_one')
    assert copied == source
    copied['attached_from_workspace'].clear()
    assert source['attached_from_workspace']


@pytest.mark.parametrize('fault', ['count', 'aggregate', 'profile_syntax', 'mismatch'])
def test_source_limits_and_exact_reference(fault):
    source, receipt = source_and_receipt()
    if fault in ('count', 'aggregate'):
        count = 33 if fault == 'count' else 10
        refs = ['artifact:' + str(i) for i in range(count)]
        receipt['items'][0]['evidence'] = [{'ref': ref} for ref in refs]
        source['attached_from_workspace'] = {ref: 'D' * 2048 for ref in refs}
    elif fault == 'profile_syntax':
        source['ref'] = '@task:work/build@owner/t_one'
    else:
        source['ref'] = '@task:build@other/t_one'
    with pytest.raises(ValueError):
        validate_receipt_source(source, receipt, expected_ref='@task:build@owner/t_one')


def test_superseded_reference_can_carry_existing_relocation():
    source, receipt = source_and_receipt()
    receipt['items'][0].update(disposition='superseded', superseded_by='artifact:successor')
    source['attached_from_workspace'] = {'artifact:successor': '/attached/successor'}
    assert validate_receipt_source(source, receipt) == source
