"""Manifest schema guards, including Python's bool/int equality ambiguity."""
import pytest

from hermes_cli.kanban_priority_manifest import validate
from hermes_cli.kanban_priority_migration import plan_global


def plan():
    return plan_global([
        {'id': 'a', 'created_on': 'one', 'priority': 10000, 'status': 'ready'},
        {'id': 'b', 'created_on': 'two', 'priority': 50000, 'status': 'done'},
    ], board_id='board', exceptions=['a', 'b'])


@pytest.mark.parametrize('case, message', [
    ('board', 'missing board identity'), ('version_type', 'noninteger policy version'),
    ('rows_type', 'rows and exceptions must be lists'), ('row_shape', 'invalid row'),
    ('row_identity', 'missing row identity'), ('row_value', 'noninteger priority'),
    ('exception_shape', 'invalid exception'), ('exception_identity', 'missing exception identity'),
    ('exception_value', 'invalid exception value or status'),
    ('terminal_open', 'terminal exception is in open inventory'),
    ('active_drift', 'exception differs from open inventory'), ('extra', 'unknown fields'),
])
def test_schema_rejects_malformed_values(case, message):
    manifest = plan()
    if case == 'board':
        manifest['board_id'] = 1
    if case == 'version_type':
        manifest['version'] = 2.0
    if case == 'rows_type':
        manifest['rows'] = tuple(manifest['rows'])
    if case == 'row_shape':
        manifest['rows'][0]['extra'] = 1
    if case == 'row_identity':
        manifest['rows'][0]['id'] = 1
    if case == 'row_value':
        manifest['rows'][0]['new'] = 900.0
    if case == 'exception_shape':
        manifest['exceptions'][0]['extra'] = 1
    if case == 'exception_identity':
        manifest['exceptions'][1]['origin'] = 1
    if case == 'exception_value':
        manifest['exceptions'][1]['status'] = 'unknown'
    if case == 'terminal_open':
        manifest['exceptions'][0]['status'] = 'done'
    if case == 'active_drift':
        manifest['exceptions'][0]['old'] = 20
    if case == 'extra':
        manifest['extra'] = 1
    with pytest.raises(ValueError, match=message):
        validate(manifest)


def test_valid_plan_roundtrip():
    manifest = plan()
    assert validate(manifest) == manifest
