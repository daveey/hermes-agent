"""Turn evidence affects ranking and inheritance, never stored priorities."""
import json

import pytest

from hermes_cli import kanban_admission_score as score
from hermes_cli import kanban_host_counts as hosts
from tests.hermes_cli import test_kanban_score_sources as helpers

sources = helpers.sources


@pytest.fixture
def observation(sources, monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home

    # Match the real reader's configured home without replacing the reader.
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = get_hermes_home() / 'cache' / 'kanban-turn-urgency-research.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {'version': 1, 'observed_at': 990, 'valid_until': 1110,
             'board': 'research', 'source_revision': 'revision',
             'owners': [{'task_id': 'owner', 'source': 'core-lane', 'ref': 'lane',
                         'head': 'head', 'expires_at': 1120}]}
    path.write_text(json.dumps(value))
    local, _ = sources
    monkeypatch.setattr(hosts, 'board_of', lambda conn: 'research' if conn is local else 'wrong')
    monkeypatch.setattr('time.time', lambda: 1000)
    return path, value


@pytest.mark.parametrize('remote', [False, True])
def test_owner_floor_ranking_inheritance_and_no_write(sources, observation, remote):
    local, replica = sources
    source = replica if remote else local
    owner = helpers.add(source, 'owner', 1000)
    (_, parts), = score.rank(local, [owner], now_ms=1000000, weights={})
    assert parts.effective_priority == 900
    assert score.effective_priorities(local, ['owner']) == {'owner': 900}
    assert source.execute("SELECT priority FROM tasks WHERE id='owner'").fetchone()[0] == 0


@pytest.mark.parametrize('remote', [False, True])
def test_owner_ranks_ahead_of_aged_cards_at_the_cap(sources, observation, remote):
    """t_01a1e023229f1073: a floor of 900 does nothing for an owner already at 1000; the turn ranks it first."""
    local, replica = sources
    source = replica if remote else local
    aged = [helpers.add(source, f'aged-{i}', 1 + i, 1000) for i in range(5)]  # ready ~16 min: +400 points
    owner = helpers.add(source, 'owner', 999, 1000)
    source.execute("INSERT INTO task_events VALUES ('owner', 'promoted', '{}', 999)")  # just back from a park
    ranked = score.rank(local, [*aged, owner], now_ms=1000000)
    assert ranked[0][0]['id'] == 'owner'
    parts = dict((row['id'], p) for row, p in ranked)
    assert parts['owner'].turn_owner and not parts['aged-0'].turn_owner
    assert parts['owner'].total < parts['aged-0'].total  # the score itself is unchanged: the turn is a tier
    assert 'turn owner' in score.explain(parts['owner'].as_dict())
    assert 'turn owner' not in score.explain(parts['aged-0'].as_dict())


def test_two_owners_keep_score_order_between_them(sources, observation):
    local, _ = sources
    path, value = observation
    value['owners'].append({'task_id': 'owner-2', 'source': 'state-turn', 'ref': 'turn', 'head': 'h',
                            'expires_at': 1120})
    path.write_text(json.dumps(value))
    first = helpers.add(local, 'owner', 999, 1000)
    second = helpers.add(local, 'owner-2', 100, 1000)  # waited longer
    other = helpers.add(local, 'aged', 1, 1000)
    ranked = score.rank(local, [first, other, second], now_ms=1000000)
    assert [row['id'] for row, _ in ranked] == ['owner-2', 'owner', 'aged']


@pytest.mark.parametrize('change', ['stale', 'future', 'foreign', 'unknown_board'])
def test_invalid_observation_never_promotes(sources, observation, monkeypatch, change):
    local, _ = sources
    path, value = observation
    row = helpers.add(local, 'owner', 1000)
    if change == 'stale':
        value['valid_until'] = 1000
    elif change == 'future':
        value['observed_at'] = 1001
    elif change == 'foreign':
        value['board'] = 'default'
    else:
        monkeypatch.setattr(hosts, 'board_of', lambda conn: None)
    path.write_text(json.dumps(value))
    (_, parts), = score.rank(local, [row], now_ms=1000000, weights={})
    assert parts.effective_priority == 0
    assert score.effective_priorities(local, ['owner']) == {'owner': 0}


def test_owner_floor_donates_but_terminal_owner_does_not(sources, observation):
    local, _ = sources
    parent = helpers.add(local, 'parent', 1000)
    helpers.add(local, 'owner', 1000)
    local.execute("INSERT INTO task_links VALUES ('parent', 'owner')")
    assert score.effective_priorities(local, ['parent']) == {'parent': 900}
    local.execute("UPDATE tasks SET status='done' WHERE id='owner'")
    assert score.effective_priorities(local, ['parent']) == {'parent': 0}
    (_, parts), = score.rank(local, [parent], now_ms=1000000, weights={})
    assert parts.effective_priority == 0


def test_rank_uses_supplied_clock_not_wall_clock(sources, observation):
    local, _ = sources
    row = helpers.add(local, 'owner', 1000)
    (_, parts), = score.rank(local, [row], now_ms=1110000, weights={})
    assert parts.effective_priority == 0


def test_owner_floor_keeps_higher_priority(sources, observation):
    local, _ = sources
    row = helpers.add(local, 'owner', 1000, 950)
    (_, parts), = score.rank(local, [row], now_ms=1000000, weights={})
    assert parts.effective_priority == 950


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_terminal_owner_itself_has_no_floor(sources, observation, status):
    local, _ = sources
    helpers.add(local, 'owner', 1000)
    local.execute('UPDATE tasks SET status=? WHERE id=?', (status, 'owner'))
    assert score.effective_priorities(local, ['owner']) == {'owner': 0}


def test_real_connection_identity_beats_ambient_board(tmp_path, monkeypatch):
    from contextlib import closing
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    kb.init_db()
    kb.create_board('research')
    with closing(kbc.connect(kb.kanban_db_path(board='research'))) as conn:
        tid = kb.create_task(conn, title='owner')
        cache = tmp_path / 'cache' / 'kanban-turn-urgency-research.json'
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps({'version': 1, 'observed_at': 990, 'valid_until': 1110,
            'board': 'research', 'source_revision': 'revision', 'owners': [
                {'task_id': tid, 'source': 'state-turn', 'ref': 'turn', 'head': 'head', 'expires_at': 1120}]}))
        monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
        assert hosts.board_of(conn) == 'research'
        (_, parts), = score.rank(conn, [{'id': tid}], now_ms=1000000, weights={})
        assert parts.effective_priority == 900


def test_unknown_owner_does_not_create_task(sources, observation):
    local, _ = sources
    assert score.effective_priorities(local, ['owner']) == {}
