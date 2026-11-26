"""Same-owner obligations are read in one batched pass, with the single-card read's exact answers."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin as origin, kanban_origin_local as local, kanban_origin_ledger as ledger
from tests.hermes_cli import test_kanban_local_origin_projection as support

world = support.world
board = support.board

STATES = ('ready', 'done', 'done_archived', 'archived', 'withdrawn', 'blocked', 'scheduled', 'raw_done',
          'missing_marker', 'wrong_marker', 'goal_done')


def _withdraw(conn, tid, _binding):
    kb._append_event(conn, tid, 'request_withdrawn', {'notice_version': 1, 'recipients': [{'chat': 'c'}]})


def _block(conn, tid, _binding):
    assert kb.block_task(conn, tid, reason='Which region?', kind='needs_input')


def _wrong_marker(conn, tid, binding):
    marker = {**binding, 'origin': {**binding['origin'], 'session': 'unrelated'}}
    marker.pop('publication_version')
    conn.execute('UPDATE origin_creation_intents SET binding_json=? WHERE task_id=?', (json.dumps(marker), tid))


def _goal_done(conn, tid, _binding):
    assert kb.complete_task(conn, tid, summary='Built and tested')
    conn.execute('UPDATE tasks SET goal_mode=1 WHERE id=?', (tid,))


CHANGES = {
    'withdrawn': _withdraw, 'blocked': _block, 'wrong_marker': _wrong_marker, 'goal_done': _goal_done,
    'scheduled': lambda conn, tid, _: conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,)),
    'raw_done': lambda conn, tid, _: conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,)),
    'missing_marker': lambda conn, tid, _: conn.execute('DELETE FROM origin_creation_intents WHERE task_id=?', (tid,)),
}


def seed(world, monkeypatch, states):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    path, replica, template = world
    template['origin']['gateway'] = 'owner'
    tasks = {}
    for i, state in enumerate(states):
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title=f'Build {i}', assignee='default', completion_contract='local-only',
                                 origin_intent={'request_id': f'batch-create-{i:03d}',
                                                'owner': {'gateway': 'owner', 'profile': 'default', 'board': 'default'},
                                                'origin': template['origin']})
            binding = json.loads(conn.execute('SELECT binding_json FROM origin_creation_intents WHERE task_id=?',
                                              (tid,)).fetchone()[0])
        binding['publication_version'] = 3
        support.publish(world[1], binding)
        support.accept((path, replica, binding), gateway='owner', request_id=f'batch-accept-{i:03d}')
        tasks[tid] = (state, binding)
    # Lifecycle changes only after every card exists.
    for tid, (state, binding) in tasks.items():
        with kbc.connect_closing() as conn:
            if state in ('done', 'done_archived', 'missing_marker', 'wrong_marker'):
                assert kb.complete_task(conn, tid, summary='Built and tested')
            if state in ('done_archived', 'archived', 'withdrawn'):
                assert kb.archive_task(conn, tid)
            CHANGES.get(state, lambda *_: None)(conn, tid, binding)
    return {tid: state for tid, (state, _) in tasks.items()}


def project(world, monkeypatch, flag):
    monkeypatch.setenv('HERMES_ORIGIN_BATCH_READS', flag)
    return ledger.project(world[0], replica=None, gateway='owner', profile='default', now=5000)


def count_board_statements(monkeypatch):
    seen = []
    original_open = origin._open_ro
    def opener(path):
        conn = original_open(path)
        if path == kb.kanban_db_path('default'):
            conn.set_trace_callback(seen.append)
        return conn
    monkeypatch.setattr(origin, '_open_ro', opener)
    return seen


def test_batched_pass_matches_single_card_reads_for_every_state(world, board, monkeypatch):
    tasks = seed(world, monkeypatch, STATES)
    single = project(world, monkeypatch, '0')
    batched = project(world, monkeypatch, '1')
    assert json.dumps(batched, sort_keys=True) == json.dumps(single, sort_keys=True)
    by_state = {tasks[row['id']]: row for row in batched}
    assert set(by_state) == set(STATES)
    assert {s: by_state[s]['status'] for s in STATES} == {
        'ready': 'ready', 'done': 'done', 'done_archived': 'archived', 'archived': 'archived',
        'withdrawn': 'archived', 'blocked': 'blocked', 'scheduled': 'scheduled', 'raw_done': 'unknown',
        'missing_marker': 'unknown', 'wrong_marker': 'unknown', 'goal_done': 'unknown'}
    assert not by_state['done']['unfinished'] and not by_state['done_archived']['unfinished']
    assert by_state['archived']['closed_uncompleted'] and by_state['goal_done']['unfinished']


def test_batched_pass_statement_count_does_not_grow_with_cards(world, board, monkeypatch):
    seed(world, monkeypatch, ('done', 'done_archived', 'archived', 'withdrawn') * 4)
    seen = count_board_statements(monkeypatch)
    single = project(world, monkeypatch, '0')
    per_card = len(seen)
    del seen[:]
    batched = project(world, monkeypatch, '1')
    assert batched == single and len(batched) == 16
    assert per_card >= 16 * 7
    # One snapshot (BEGIN, ROLLBACK) and six batched reads, plus the published recipients of each of the four
    # withdrawn cards.
    assert len(seen) <= 8 + 4, [s[:90] for s in seen]


@pytest.mark.parametrize('fault', ['prefetch', 'second_card', 'second_card_value'])
def test_failed_batched_read_falls_back_to_single_card_reads(world, board, monkeypatch, fault):
    seed(world, monkeypatch, ('ready', 'done', 'blocked', 'archived'))
    single = project(world, monkeypatch, '0')
    if fault == 'prefetch':
        def broken(conn, ids):
            raise sqlite3.OperationalError('database is locked')
        monkeypatch.setattr(local, '_prefetch', broken)
    else:
        original, calls = local._observe_prefetched, []
        def flaky(conn, binding, read, **kw):
            calls.append(binding['owner']['task_id'])
            if len(calls) == 2:
                raise sqlite3.OperationalError('disk I/O error') if fault == 'second_card' else ValueError('bad')
            return original(conn, binding, read, **kw)
        monkeypatch.setattr(local, '_observe_prefetched', flaky)
    alone = []
    original_single = local.observe_local_binding
    def observe(binding, **kw):
        alone.append(binding['owner']['task_id'])
        return original_single(binding, **kw)
    monkeypatch.setattr(local, 'observe_local_binding', observe)
    batched = project(world, monkeypatch, '1')
    if fault == 'second_card_value':
        # A card's own bad evidence is that card's unknown, as in the single-card read; nothing is re-read.
        assert alone == []
        assert [r['status'] for r in batched] == [s if i != 1 else 'unknown'
                                                  for i, s in enumerate(r['status'] for r in single)]
    else:
        assert batched == single
        assert len(alone) == (4 if fault == 'prefetch' else 3)


def test_two_obligations_on_one_card_each_get_their_own_answer(world, board, monkeypatch):
    tasks = seed(world, monkeypatch, ('done', 'ready'))
    done = next(t for t, s in tasks.items() if s == 'done')
    with sqlite3.connect(world[0]) as conn:
        key, _digest, raw, resolved, task, version, released = conn.execute(
            'SELECT * FROM origin_obligations WHERE binding_json LIKE ?', (f'%{done}%',)).fetchone()
        # A second accepted obligation whose marker the owner board does not hold: same card, unknown.
        other = {**json.loads(raw), 'binding_id': 'binding-other'}
        conn.execute('INSERT INTO origin_obligations VALUES (?,?,?,?,?,?,?)',
                     (key + 'x', ledger.binding_hash(other), ledger._json(other), resolved, task, version, released))
    single = project(world, monkeypatch, '0')
    batched = project(world, monkeypatch, '1')
    assert batched == single
    assert sorted(r['status'] for r in batched if r['id'] == done) == ['done', 'unknown']
