import json
import sqlite3
import pytest
from hermes_cli import kanban_db_connect as dbc, kanban_actionability_transport as act
from tests.hermes_cli import test_home_typed_capture as support
live = support.live
fixture, mirror, outbound = support.fixture, support.mirror, support.outbound


@pytest.mark.parametrize('captured', [False, True])
@pytest.mark.parametrize('identity', ['primary', 'secondary'])
def test_replace_cannot_rebind_captured_epoch_without_changing_request_bytes(live, captured, identity):
    task, reserve = fixture(live, 'actionability')
    if captured:
        mirror(live, task, 'committed-nonce-0001')
    with dbc.write_txn(live):
        owned = reserve()
    original = outbound(live, 'actionability')
    replaced = dict(owned, home_generations=json.dumps({task: 'committed-nonce-0002'}))
    if identity == 'secondary':
        replaced['op_id'] = 'different-primary-same-contract'
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with dbc.write_txn(live):
            live.execute('UPDATE kanban_actionability_commands SET home_generations=?', (replaced['home_generations'],))
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with dbc.write_txn(live):
            columns = ','.join(replaced)
            values = ','.join('?' for _ in replaced)
            live.execute(f'INSERT OR REPLACE INTO kanban_actionability_commands ({columns}) VALUES ({values})', tuple(replaced.values()))
    after = act.command(live, owned['op_id'])
    assert after['request_json'] == owned['request_json']
    assert after['request_sha256'] == owned['request_sha256']
    assert outbound(live, 'actionability') == original
