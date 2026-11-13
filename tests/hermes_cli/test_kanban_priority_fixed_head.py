"""Capture and verify read the inventory at ONE fixed head, closed by construction.

The Board pages tasks by id (1000 per call) and holds no read transaction across
calls, so a capture over 14 pages spans 55-180 s of a board that creates about
one card a minute. New ULID-style ids (``t_01a1d4…``) sort before the legacy
ids (``t_c21c…``), so the page covering a new card was usually read before the
card existed: 2026-11-13 MacBook captures refused 5 of 6 (95fe) and 2 of 3
(99ae) times with ``missing from snapshot, open at target`` (t_01a1d3f04e37f0cf,
t_01a1d4087cc6545b). The feed replay from the first page head now admits a card
whose first in-window change is the Board's creation row (``version`` 1), so
the result is the inventory at the final head. Real priority, origin, identity
and delete changes still refuse.
"""
import copy
import random

import pytest

from hermes_cli import kanban_priority_prepare as prepare
from hermes_cli.kanban_priority import PRIORITY_CAP
from hermes_cli.kanban_priority_feed import check_interval
from tests.hermes_cli.test_kanban_priority_window_churn import Window, _row, _task

TERMINAL = ('done', 'archived')


def _new(seq, uid, version=1, **over):
    return _task(seq, uid, **{**_row(uid, priority=500), 'version': version, **over})


def _rows(manifest):
    return [(row['id'], row['old']) for row in manifest['rows']]


# ---- unit shapes (Window: page heads 1 then 3, final head 5) ------------------------------------------------


@pytest.mark.parametrize('seq', [2, 3, 4, 5])
def test_card_created_mid_window_and_still_open_joins_inventory(seq):
    hub = Window([_row('a'), _row('b')], [_new(seq, 'n')])
    assert _rows(prepare.capture(hub, board_id='board', exceptions=[])) == [
        ('a', 10000), ('b', 10000), ('n', 500)]


def test_card_created_completed_and_reopened_mid_window_joins_inventory():
    hub = Window([_row('a')], [_new(2, 'n'), _task(3, 'n', status='done'), _task(4, 'n', status='ready')])
    assert _rows(prepare.capture(hub, board_id='board', exceptions=[])) == [('a', 10000), ('n', 500)]


def test_card_created_at_the_cap_is_accepted():
    hub = Window([_row('a')], [_new(2, 'n', priority=PRIORITY_CAP)])
    assert ('n', PRIORITY_CAP) in _rows(prepare.capture(hub, board_id='board', exceptions=[]))


@pytest.mark.parametrize('terminal', TERMINAL)
def test_card_created_above_cap_and_finished_mid_window_is_out_of_scope(terminal):
    hub = Window([_row('a')], [_new(2, 'n', priority=PRIORITY_CAP + 1), _task(4, 'n', status=terminal)])
    assert _rows(prepare.capture(hub, board_id='board', exceptions=[])) == [('a', 10000)]


@pytest.mark.parametrize('changes', [
    pytest.param([_new(2, 'n', version=3)], id='pre-existing-full-row'),
    pytest.param([_new(2, 'n', version=None)], id='pre-existing-no-version'),
    pytest.param([_new(2, 'n', version=True)], id='bool-version'),
    pytest.param([_new(2, 'n', version=1.0)], id='float-version'),
    pytest.param([_task(2, 'n', status='ready', version=1)], id='patch-shaped'),
    pytest.param([_task(2, 'n', title='renamed'), _new(4, 'n')], id='creation-after-update'),
    pytest.param([_task(2, 'n', status='done'), _new(4, 'n')], id='creation-after-terminal-update'),
    pytest.param([_new(2, 'n', id='other')], id='identity-mismatch'),
    pytest.param([_new(2, 'n', priority=PRIORITY_CAP + 1)], id='above-cap'),
    pytest.param([_new(2, 'n', priority=None)], id='no-priority'),
    pytest.param([_new(2, 'n', priority=True)], id='bool-priority'),
    pytest.param([_new(2, 'n', created_on='')], id='no-origin'),
    pytest.param([_new(2, 'n', created_on=None)], id='null-origin'),
    pytest.param([_new(2, 'n', created_on=5)], id='int-origin'),
    pytest.param([_task(2, 'n', id='n', version=1, created_on='one', priority=5)], id='creation-without-status'),
    pytest.param([_new(2, 'n'), _task(4, 'n', priority=7)], id='priority-changed-after-creation'),
    pytest.param([_new(2, 'n'), _task(4, 'n', created_on='two')], id='origin-changed-after-creation'),
    pytest.param([_new(2, 'n'), _task(4, 'n', op='delete')], id='deleted-after-creation'),
    pytest.param([_new(2, 'n'), _task(3, 'n', status='done'), _task(4, 'n', priority=7)],
                 id='priority-changed-then-terminal'),
])
def test_missing_card_that_is_not_an_admissible_creation_refuses(changes):
    hub = Window([_row('a'), _row('b')], changes)
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(hub, board_id='board', exceptions=[])


def test_creation_after_final_head_is_ignored():
    hub = Window([_row('a')], [])
    hub.log.append(_new(6, 'n'))
    assert _rows(prepare.capture(hub, board_id='board', exceptions=[])) == [('a', 10000)]


def test_created_rows_join_the_inventory_in_id_order():
    rows = [_row('b'), _row('d')]
    check_interval(Window(rows, [_new(2, 'e'), _new(3, 'a'), _new(4, 'c')]), 'board', rows, 1, 5)
    assert [row['id'] for row in rows] == ['a', 'b', 'c', 'd', 'e']


def test_verify_accepts_a_card_created_during_its_own_read():
    manifest = prepare.capture(Window([_row('a'), _row('b')], []), board_id='board', exceptions=[])
    hub = Window([_row('a'), _row('b')], [_new(3, 'n')])
    assert prepare.verify(hub, manifest, undo=True)['verified'] == 2


def test_verify_refuses_a_card_created_above_the_cap_during_its_read():
    manifest = prepare.capture(Window([_row('a'), _row('b')], []), board_id='board', exceptions=[])
    hub = Window([_row('a'), _row('b')], [_new(3, 'n', priority=PRIORITY_CAP + 1)])
    with pytest.raises(ValueError, match='changed'):
        prepare.verify(hub, manifest, undo=True)


# ---- simulated busy board: churn across every page ----------------------------------------------------------


class BusyBoard:
    """A Board-shaped fake: full-row feed entries with versions, id-paged snapshot, churn before every read.

    Each ``snapshot_page`` call (the final ``limit=1`` read included) first applies a burst of
    creations (new ids sort before AND among legacy ids), completions, reopenings, title edits,
    create-then-finish cards and unrelated table traffic. ``truth`` is the open inventory at the
    final observed head. ``inject`` puts one real priority change on a pre-existing open card
    before page ``inject``.
    """

    def __init__(self, seed, *, inject=None, ahead=False, page=6, legacy=60):
        self.rng = random.Random(seed)
        self.ahead = ahead
        self.seq = 0
        self.log = []
        self.tasks = {}
        self.page = page
        self.reads = 0
        self.inject = inject
        self.injected = None
        self.first = 0
        self.reopen = True
        self.truth = None
        for i in range(legacy):
            uid = f't_{self.rng.randrange(0xc0000000, 0xffffffff):08x}'
            status = self.rng.choice(['ready', 'running', 'todo', 'blocked', 'done', 'archived', 'done'])
            self._put(uid, {'id': uid, 'status': status, 'created_on': self.rng.choice(['hub', 'mbp', 'mini']),
                             'priority': self.rng.choice([0, 5, 10, 100, 900, 10000, 60320]), 'title': str(i)})
        self.ulid = 0x01a1d3000000

    def _put(self, uid, patch):
        row = self.tasks.get(uid)
        row = {'uid': uid, 'version': 0} if row is None else row
        self.seq += 1
        row.update(patch, version=row['version'] + 1, seq=self.seq, deleted_at=None)
        self.tasks[uid] = row
        self.log.append({'seq': self.seq, 'table': 'tasks', 'uid': uid, 'op': 'upsert', 'fields': copy.deepcopy(row)})

    def _noise(self):
        self.seq += 1
        self.log.append({'seq': self.seq, 'table': self.rng.choice(['task_events', 'task_runs']), 'uid': 'x',
                         'op': 'upsert', 'fields': {}})

    def _open(self):
        return [u for u, r in sorted(self.tasks.items()) if r['status'] not in TERMINAL]

    def _churn(self):
        for _ in range(self.rng.randrange(2, 8)):
            kind = self.rng.choice(['create', 'create', 'finish', 'reopen', 'edit', 'flash', 'noise'])
            if kind in ('create', 'flash'):
                self.ulid += self.rng.randrange(1, 5000)
                uid = f't_{self.ulid:012x}{self.rng.randrange(0x10000):04x}'
                if self.rng.random() < 0.3:  # ids that interleave with the legacy pages too
                    uid = f't_{self.rng.randrange(0xc0000000, 0xffffffff):08x}n'
                self._put(uid, {'id': uid, 'status': self.rng.choice(['ready', 'triage', 'todo']),
                                'created_on': self.rng.choice(['hub', 'worker-3', 'mbp']),
                                'priority': self.rng.choice([0, 50, 500, PRIORITY_CAP]), 'title': 'new'})
                if kind == 'flash':
                    self._put(uid, {'status': 'running'})
                    self._put(uid, {'status': 'done'})
            elif kind == 'finish' and self._open():
                self._put(self.rng.choice(self._open()), {'status': self.rng.choice(TERMINAL)})
            elif kind == 'reopen':
                closed = [u for u, r in sorted(self.tasks.items()) if r['status'] == 'done'
                          and (self.reopen or r['priority'] <= PRIORITY_CAP)]
                if closed:
                    self._put(self.rng.choice(closed), {'status': 'ready'})
            elif kind == 'edit':
                self._put(self.rng.choice(sorted(self.tasks)), {'title': f'edited {self.seq}'})
            else:
                self._noise()

    def snapshot_page(self, board, table, after='', limit=1000):
        self._churn()
        if self.inject is not None and self.reads >= self.inject and self.injected is None:
            # behind: a card on a page already read (the snapshot holds its old value);
            # ahead: a card unchanged since the first head, on a page not read yet.
            victims = [u for u in self._open() if (u > after and self.tasks[u]['seq'] <= self.first
                                                   if self.ahead else u <= after)]
            if victims:
                self.injected = self.rng.choice(victims)
                self._put(self.injected, {'priority': self.tasks[self.injected]['priority'] + 1})
        self.reads += 1
        if self.reads == 1:
            self.first = self.seq
        live = [copy.deepcopy(r) for u, r in sorted(self.tasks.items()) if u > after]
        if limit == 1:
            self.truth = [(u, r['priority']) for u, r in sorted(self.tasks.items()) if r['status'] not in TERMINAL]
            return {'table': 'tasks', 'head': self.seq, 'rows': live[:1], 'more': len(live) > 1,
                    'next': live[0]['id'] if live else after}
        batch = live[:self.page]
        return {'table': 'tasks', 'head': self.seq, 'rows': batch, 'more': len(live) > len(batch),
                'next': batch[-1]['id'] if batch else after}

    def changes(self, board, since):
        batch = [c for c in self.log if c['seq'] > since][:7]
        return {'changes': copy.deepcopy(batch), 'next': batch[-1]['seq'] if batch else since,
                'head': self.seq, 'more': bool(batch) and batch[-1]['seq'] < self.seq}


SEEDS = range(20)


def test_busy_board_creations_land_in_pages_already_read():
    # Guard the simulation itself: it must produce the incident shape.
    hits = 0
    for seed in SEEDS:
        board = BusyBoard(seed)
        prepare.capture(board, board_id='board', exceptions=[])
        first = [c for c in board.log if c['fields'].get('version') == 1 and c['fields'].get('title') == 'new']
        hits += any(c['uid'] < max(board.tasks) for c in first)
    assert hits == len(SEEDS)


@pytest.mark.parametrize('seed', SEEDS)
def test_busy_board_capture_is_the_inventory_at_the_final_head(seed):
    board = BusyBoard(seed)
    manifest = prepare.capture(board, board_id='board', exceptions=[])
    assert _rows(manifest) == board.truth


@pytest.mark.parametrize('seed', SEEDS)
def test_busy_board_verify_reads_back_through_churn(seed):
    board = BusyBoard(seed)
    manifest = prepare.capture(board, board_id='board', exceptions=[])
    # Later churn keeps going during verify's own paged read. Reopening an
    # above-cap legacy card is a real apply-time refusal, so verify's window
    # reopens only capped cards here.
    board.rng, board.reopen = random.Random(seed + 1000), False
    result = prepare.verify(board, manifest, undo=True)
    still = {u for u, _ in board.truth}
    assert result['verified'] == len([r for r in manifest['rows'] if r['id'] in still])


@pytest.mark.parametrize('seed', SEEDS)
def test_busy_board_real_priority_change_mid_window_still_refuses(seed):
    board = BusyBoard(seed, inject=1 + seed % 8)
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(board, board_id='board', exceptions=[])
    assert board.injected is not None


def test_busy_board_priority_change_before_its_page_is_read_is_the_fixed_head_value():
    # A row paged AFTER its in-window change already holds the final-head
    # value, so the capture is exact; only a change the snapshot missed would
    # leave it stale, and that refuses (above).
    for seed in SEEDS:
        board = BusyBoard(seed, inject=1, ahead=True)
        manifest = prepare.capture(board, board_id='board', exceptions=[])
        assert board.injected is not None and _rows(manifest) == board.truth


# ---- review follow-ups (PR #2025 nits 1-3) ------------------------------------------------------------------


@pytest.mark.parametrize('terminal', TERMINAL)
def test_card_created_and_finished_in_window_resolves_an_exception_like_a_later_page(terminal):
    # Nit 2: a create-then-finish card was dropped from the rows, so an
    # exceptions entry naming it refused ('unresolved exception') only when its
    # page was read before the creation. It is now a terminal row either way.
    changes = [_new(2, 'n'), _task(4, 'n', status=terminal)]
    early = prepare.capture(Window([_row('a')], changes), board_id='board', exceptions=['n'])
    late = prepare.capture(Window([_row('a'), {**_row('n', priority=500), 'status': terminal}], changes),
                           board_id='board', exceptions=['n'])
    assert early == late
    assert early['exceptions'] == [{'id': 'n', 'status': terminal, 'origin': 'one', 'old': 500}]
    assert _rows(early) == [('a', 10000)]


def test_terminal_arrival_joins_the_rows_in_id_order():
    rows = [_row('b')]
    check_interval(Window(rows, [_new(2, 'a'), _task(3, 'a', status='done')]), 'board', rows, 1, 5)
    assert [(row['id'], row['status']) for row in rows] == [('a', 'done'), ('b', 'ready')]


@pytest.mark.parametrize('seed', SEEDS)
def test_busy_board_inventory_rows_are_every_card_at_the_final_head(seed):
    # The fixed-head invariant over ALL statuses, not just the open manifest
    # rows: create-then-finish ("flash") cards are terminal rows, as a later
    # page would show them.
    board = BusyBoard(seed)
    rows = prepare._inventory(board, 'board')
    assert [(r['id'], r['status'], r['priority']) for r in rows] == [
        (u, r['status'], r['priority']) for u, r in sorted(board.tasks.items())]


@pytest.mark.parametrize('earlier', [
    pytest.param(lambda: _task(2, 'b', **{**_row('b'), 'title': 'edited'}), id='full-row-edit-with-old-priority'),
    pytest.param(lambda: _task(2, 'b', priority=10000), id='patch-with-old-priority'),
])
def test_priority_change_after_an_earlier_in_window_old_value_refuses_even_paged_late(earlier):
    # Nit 1: "exact" holds for a row's ONLY in-window priority change. An
    # earlier in-window change still carrying the old priority disagrees with
    # the late-paged row (which already holds the new value), so capture
    # fails closed rather than replaying priority.
    hub = Window([_row('a'), _row('b', priority=7)], [earlier(), _task(3, 'b', priority=7)])
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(hub, board_id='board', exceptions=[])


def test_only_in_window_priority_change_paged_late_is_exact():
    hub = Window([_row('a'), _row('b', priority=7)], [_task(3, 'b', priority=7)])
    assert _rows(prepare.capture(hub, board_id='board', exceptions=[])) == [('a', 10000), ('b', 7)]


def test_above_cap_card_paged_before_the_window_is_planned_but_an_above_cap_arrival_refuses():
    # Nit 3: deliberate asymmetry. A paged above-cap card is in the snapshot
    # and normalized; one created in the window is an arrival and refuses, as
    # kanban_priority_inventory.classify refuses it at apply time.
    paged = prepare.capture(Window([_row('a'), _row('n', priority=PRIORITY_CAP + 1)], []),
                            board_id='board', exceptions=[])
    assert [row['id'] for row in paged['rows']] == ['a', 'n']
    assert all(row['new'] <= paged['ceiling'] for row in paged['rows'])
    with pytest.raises(ValueError, match='exceeds cap'):
        prepare.capture(Window([_row('a')], [_new(2, 'n', priority=PRIORITY_CAP + 1)]),
                        board_id='board', exceptions=[])
