"""A rejected standing owner is not repaired merely by starting another run."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_placement as kp
from tests.hermes_cli import test_kanban_forward_retry_boundary as helpers


@pytest.mark.parametrize('synthetic', [True, False])
def test_later_run_keeps_synthetic_binding_refusal_without_another_send(tmp_path, monkeypatch, synthetic):
    session = 'kanban_owner_pressure' if synthetic else 's1'
    options = {'idempotency_key': 'product-key'}
    original_id = kp.request_id('w', options, session, options)
    result, body = helpers._history()
    body['args']['origin']['session'] = session
    path = tmp_path / 'hub.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sent (id TEXT PRIMARY KEY, state TEXT, result TEXT, body TEXT)')
        db.execute('INSERT INTO sent VALUES (?,?,?,?)', (
            original_id, 'rejected', json.dumps(result), json.dumps(body)))
    monkeypatch.setattr(kp.reg, 'hub_db_path', lambda: path)
    monkeypatch.setattr(kp, 'hub_carries_options', lambda cli: True)
    calls = []

    def send(argv):
        calls.append(argv)
        return 0, '', ''

    monkeypatch.setattr(kp, 'hub_runner', send)
    before = path.read_bytes()
    for run in ('run-2', 'run-3'):
        out = kp.create_remote(options, target='hub', self_gw='w', origin_session=session,
                               origin_profile='default', cfg={}, origin_gateway='hub',
                               creator=helpers.CREATOR, run_uid=run)
        if synthetic:
            assert out.get('ok') is False, out
            assert out['fatal'] is True
            assert out['request_id'] == original_id
            assert out['code'] == 'origin_forward_binding_missing'
            assert 'custody' in out['error']
        else:
            assert out.get('ok') is True, out
            assert out['request_id'] != original_id
    assert len(calls) == (0 if synthetic else 2)
    assert path.read_bytes() == before
