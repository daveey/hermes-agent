"""Immutable courier refusals settle; empty auto lease labels are not a hub pin."""
import json

import pytest

from hermes_cli.kanban_origin_owner import _dispatch_result


@pytest.mark.parametrize('code', ['placement_unavailable', 'create_overlap', 'requires_declaration_missing'])
@pytest.mark.parametrize('handoff', [{}, {'handoff_complete': False}])
def test_typed_no_create_is_terminal(code, handoff):
    raw = {'error': 'No card was created', 'created': False, 'code': code, **handoff}
    result = _dispatch_result(json.dumps(raw))
    assert result['status'] == 'rejected'
    assert result['code'] == code
    assert result['created'] is False
    assert result['handoff_complete'] is False


@pytest.mark.parametrize('raw', [
    'not JSON', 'null', '[]', json.dumps({'error': 'exception after unknown write'}),
    json.dumps({'code': 'placement_unavailable'}),
    json.dumps({'created': False}), json.dumps({'created': False, 'code': ''}),
    json.dumps({'created': False, 'code': 17}),
    json.dumps({'created': 0, 'code': 'placement_unavailable'}),
    json.dumps({'created': True, 'code': 'placement_unavailable'}),
])
def test_unknown_write_stays_retryable(raw):
    assert _dispatch_result(raw) == {
        'status': 'pending', 'code': 'origin_dispatch_pending', 'handoff_complete': False}
