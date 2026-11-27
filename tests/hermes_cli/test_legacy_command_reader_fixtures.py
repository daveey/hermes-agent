"""Legacy reader fault inputs coexist with native command identity refusal."""
import sqlite3

import pytest

from tests.hermes_cli import test_kanban_sync_actionability_drift as support
from tests.hermes_cli.kanban_legacy_command_support import seed_legacy_command

stores = support.stores


@pytest.mark.parametrize('column', ['op_id', 'task_id', 'contract_key', 'phase'])
def test_legacy_seed_preserves_guard_and_all_unmodified_bytes(stores, column):
    live, _, _ = stores
    before = dict(live.execute('SELECT * FROM kanban_actionability_commands').fetchone())
    dump = sorted(live.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='captured command identity is immutable'):
        live.execute(f'UPDATE kanban_actionability_commands SET {column}=?', ('legacy-damage',))
    assert sorted(live.iterdump()) == dump
    seed_legacy_command(live, **{column: 'legacy-damage'})
    assert dict(live.execute('SELECT * FROM kanban_actionability_commands').fetchone()) == {
        **before, column: 'legacy-damage'}
    with pytest.raises(sqlite3.IntegrityError, match='captured command identity is immutable'):
        live.execute(f'UPDATE kanban_actionability_commands SET {column}=?', ('later-rekey',))


def test_reader_seed_refuses_epoch_bearing_custody(stores):
    live, _, _ = stores
    row = dict(live.execute('SELECT * FROM kanban_actionability_commands').fetchone())
    live.execute('DELETE FROM kanban_actionability_commands')
    row['home_generations'] = '{"t_card":"committed-nonce-0001"}'
    live.execute('INSERT INTO kanban_actionability_commands (' + ','.join(row) + ') VALUES (' +
                 ','.join('?' for _ in row) + ')', tuple(row.values()))
    before = sorted(live.iterdump())
    with pytest.raises(AssertionError, match='never rewrite epoch-bearing custody'):
        seed_legacy_command(live, contract_key='legacy-damage')
    assert sorted(live.iterdump()) == before
