"""Stored dependency lifecycle -> canonical wait reader; no injected wait annotations."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_session_wait_rows import _local_wait_info


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    conn = kbc.connect(tmp_path / 'board.db')
    yield conn
    conn.close()


def create(conn, title, **kwargs):
    return kb.create_task(conn, title=title, assignee='default', **kwargs)


def stamp(conn, tid, kind):
    return conn.execute('SELECT created_at FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                        (tid, kind)).fetchone()[0]


def wait(conn, tid):
    return _local_wait_info(conn, [tid], now=2000000000).get(tid)


def test_creation_dependency_clock_is_source_event_not_task_age(board):
    parent = create(board, 'Parent')
    child = create(board, 'Child', parents=[parent])
    board.execute('ALTER TABLE tasks ADD COLUMN ready_since INTEGER')
    board.execute('UPDATE tasks SET created_at=1, ready_since=2 WHERE id=?', (child,))
    board.commit()
    info = wait(board, child)
    assert info is not None
    assert info['waiting_since'] == stamp(board, child, 'dependency_wait')
    assert info['waiting_on'] == 'card ' + parent
    assert info['agent_owned'] is False
    assert info == _local_wait_info(board, [child], now=2000000120)[child]


def test_effective_edge_change_resets_but_duplicate_link_and_comment_do_not(board, monkeypatch):
    parent = create(board, 'Parent')
    child = create(board, 'Child', parents=[parent])
    initial = stamp(board, child, 'dependency_wait')
    monkeypatch.setattr(kb.time, 'time', lambda: initial + 60)
    kb.link_tasks(board, parent, child)
    assert wait(board, child)['waiting_since'] == initial
    other = create(board, 'Other')
    monkeypatch.setattr(kb.time, 'time', lambda: initial + 120)
    kb.link_tasks(board, other, child)
    assert wait(board, child)['waiting_since'] == initial + 120
    monkeypatch.setattr(kb.time, 'time', lambda: initial + 180)
    kb.unlink_tasks(board, other, child)
    kb.add_comment(board, child, 'fixture', 'Unrelated comment', system=True)
    assert wait(board, child)['waiting_since'] == initial + 180


def test_recovery_clears_clock_and_new_wait_does_not_reuse_old_episode(board, monkeypatch):
    parent = create(board, 'Parent')
    child = create(board, 'Child', parents=[parent])
    initial = stamp(board, child, 'dependency_wait')
    kb.unlink_tasks(board, parent, child)
    assert wait(board, child) is None
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == 'ready'
    assert wait(board, child) is None
    monkeypatch.setattr(kb.time, 'time', lambda: initial + 180)
    kb.link_tasks(board, parent, child)
    assert kb.get_task(board, child).status == 'todo'
    assert wait(board, child)['waiting_since'] == initial + 180


def test_missing_episode_history_stays_undated_and_no_parent_is_not_wait(board):
    parent = create(board, 'Parent')
    child = create(board, 'Child', parents=[parent])
    board.execute('DELETE FROM task_events WHERE task_id=?', (child,))
    board.commit()
    assert wait(board, child)['waiting_since'] is None
    kb.unlink_tasks(board, parent, child)
    assert kb.get_task(board, child).status == 'ready'
    assert wait(board, child) is None


def test_parent_completion_then_invalidation_starts_new_episode(board, monkeypatch):
    parent = create(board, 'Parent')
    child = create(board, 'Child', parents=[parent])
    initial = stamp(board, child, 'dependency_wait')
    assert kb.complete_task(board, parent, result='Acceptance verified')
    kb.recompute_ready(board)
    assert wait(board, child) is None
    monkeypatch.setattr(kb.time, 'time', lambda: initial + 300)
    # Match the reopen surface's row flip; the descendant invalidation below is the real producer.
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='todo', completed_at=NULL WHERE id=?", (parent,))
    kb.invalidate_descendants_for_parent_reopen(board, parent, author='fixture')
    assert kb.get_task(board, child).status == 'todo'
    assert wait(board, child)['waiting_since'] == initial + 300


def test_graph_without_matching_history_is_not_dated_from_old_wait(board):
    parent = create(board, 'Parent')
    child = create(board, 'Child', parents=[parent])
    other = create(board, 'Unrecorded parent')
    board.execute('INSERT INTO task_links(parent_id,child_id) VALUES (?,?)', (other, child))
    board.commit()
    assert wait(board, child)['waiting_since'] is None


def test_unblock_dependency_entry_does_not_reuse_old_episode(board, monkeypatch):
    parent = create(board, 'Parent')
    child = create(board, 'Child', parents=[parent])
    initial = stamp(board, child, 'dependency_wait')
    kb.unlink_tasks(board, parent, child)
    assert kb.block_task(board, child, kind='transient', reason='Fixture source unavailable')
    kb.link_tasks(board, parent, child)
    monkeypatch.setattr(kb.time, 'time', lambda: initial + 300)
    assert kb.unblock_task(board, child)
    assert wait(board, child)['waiting_since'] == initial + 300


def test_claim_parent_recheck_dates_new_dependency_episode(board, monkeypatch):
    parent = create(board, 'Parent')
    assert kb.complete_task(board, parent, result='Acceptance verified')
    child = create(board, 'Child', parents=[parent])
    entry = stamp(board, child, 'created') + 300
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='todo', completed_at=NULL WHERE id=?", (parent,))
    monkeypatch.setattr(kb.time, 'time', lambda: entry)
    assert kb.claim_task(board, child) is None
    assert kb.get_task(board, child).status == 'todo'
    assert wait(board, child)['waiting_since'] == entry


def test_todo_graph_read_is_batched_and_matches_single_card_reader(board):
    """The sidebar poll path reads every todo card's parents in one statement, not one per card."""
    from hermes_cli.kanban_db_parent_states import parent_states_many

    parent = create(board, 'Parent')
    work = []
    for n in (3, 30):
        kids = [create(board, f'Child {n}-{i}', parents=[parent]) for i in range(n)]
        seen = []
        board.set_trace_callback(seen.append)
        try:
            info = _local_wait_info(board, kids, now=2000000000)
        finally:
            board.set_trace_callback(None)
        assert set(info) == set(kids) and all(v['waiting_on'] == 'card ' + parent for v in info.values())
        work.append(sum('task_links' in s for s in seen))
        many = parent_states_many(board, kids)
        assert many == {k: kb.parent_states(board, k) for k in kids}
    assert work[0] == work[1], work  # constant: no per-card graph statement
    assert parent_states_many(board, []) == {}


def _terminal(conn, tid, how):
    if how == 'done':
        assert kb.complete_task(conn, tid, result='ok')
    else:
        assert kb.archive_task(conn, tid)


@pytest.mark.parametrize('how', ['done', 'archived'])
def test_terminal_parent_restriction_check_is_batched_on_the_poll_path(board, how):
    """Every statement counts: a terminal parent's restriction evidence is read in bounded statements."""
    blocker = create(board, 'Open blocker')
    work = []
    for n in (3, 30):
        kids = []
        for i in range(n):
            done = create(board, f'Terminal {how} {n}-{i}')
            _terminal(board, done, how)
            kids.append(create(board, f'Child {how} {n}-{i}', parents=[blocker, done]))
        seen = []
        board.set_trace_callback(seen.append)
        try:
            info = _local_wait_info(board, kids, now=2000000000)
        finally:
            board.set_trace_callback(None)
        # Only the open blocker gates; the ordinary terminal parent is satisfied and not named.
        assert set(info) == set(kids) and all(v['waiting_on'] == 'card ' + blocker for v in info.values())
        work.append(len(seen))
    assert work[0] == work[1], work  # constant in board size, every statement counted


@pytest.mark.parametrize('how', ['done', 'archived'])
@pytest.mark.parametrize('kind', ['decision_hold_restricted', 'administrative_stop_restricted'])
def test_restricted_terminal_parent_still_gates_beside_ordinary_ones(board, how, kind):
    """Negative control: batching keeps held-terminal-parent semantics (restricted never counts as success)."""
    from hermes_cli.kanban_db_parent_states import parent_blocks, parent_blocks_many
    held, plain = create(board, 'Held terminal'), create(board, 'Plain terminal')
    child = create(board, 'Child', parents=[held, plain])
    other = create(board, 'Other child', parents=[plain])
    with kb.write_txn(board):
        kb._append_event(board, held, kind, {'cause': 'scope_stopped'})
    _terminal(board, held, how)
    _terminal(board, plain, how)
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == 'todo'  # the real gate agrees: held terminal is not success
    assert wait(board, child)['waiting_on'] == 'card ' + held
    assert wait(board, other) is None
    pairs = [(held, how), (plain, how), ('t_missing', None), (child, 'todo')]
    assert parent_blocks_many(board, pairs) == {p: parent_blocks(board, p, s) for p, s in pairs}
    assert parent_blocks_many(board, pairs) == {held: True, plain: False, 't_missing': True, child: True}


def test_contained_archive_parent_gates_without_restriction_events(board):
    """An archived parent whose projection is not archived (containment) gates in the batch too."""
    from hermes_cli.kanban_db_parent_states import parent_blocks, parent_blocks_many
    from hermes_cli.kanban_table_columns import table_columns
    if 'containment_episode' not in table_columns(board, 'tasks'):
        pytest.skip('legacy schema has no containment marker')
    parent = create(board, 'Contained')
    _terminal(board, parent, 'archived')
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET containment_episode='ep' WHERE id=?", (parent,))
    assert parent_blocks_many(board, [(parent, 'archived')]) == {parent: parent_blocks(board, parent, 'archived')}
    assert parent_blocks_many(board, [(parent, 'archived')]) == {parent: True}
