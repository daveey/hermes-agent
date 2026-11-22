"""An event refresh reads only the annotation parts whose own inputs moved. [daveey]

t_01a204267fa8a30d: the event-driven holder forced the counts, the links projection and every done-check verdict on
every wake. The hub's watched files are written about five times a second, mostly bookkeeping, so the full read ran
back to back (51% CPU in the ``kanban-annotation`` thread).
"""
from __future__ import annotations

import contextlib
import contextvars
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.annotation_event_loop import run
from hermes_cli.annotation_file_events import FileEvents
from tests.hermes_cli import test_kanban_annotation_host as helpers

SCOPE = ('annotation-parts-test', 'default')


@pytest.fixture
def world(tmp_path, monkeypatch):
    """One board, one plain input (state.db's stand-in) and a done check with one declared file."""
    board = tmp_path / 'kanban.db'
    conn = helpers._board(board, [('t1', 'running', 's1')])
    plain, declared = tmp_path / 'state.db', tmp_path / 'done-check.json'
    plain.write_text('0')
    declared.write_text('0')
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: SCOPE)
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [board])
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: None)
    monkeypatch.setattr(ksa, '_replica_paths', list)
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, 'core_sha', lambda: 'a' * 40)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([str(board), str(plain)], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'SETTLE_S', ())
    seen = SimpleNamespace(counts=0, links=0, checks=0, flagged=True, conn=conn, board=board, plain=plain,
                           declared=declared, deadline=None)
    recompute = ksa._recompute_all_counts

    def counting(*a, **kw):
        seen.counts += 1
        return recompute(*a, **kw)

    def project():
        seen.links += 1
        return []

    def check(card):
        seen.checks += 1
        return seen.flagged
    monkeypatch.setattr(ksa, '_recompute_all_counts', counting)
    monkeypatch.setattr(ksa, '_project_remote_links', project)
    monkeypatch.setattr(ksa, '_done_checks', {'fixture': check})
    monkeypatch.setattr(host, '_done_event_sources', {'fixture': (check, lambda: [declared], lambda: seen.deadline)})
    monkeypatch.setattr(ksa._recent_done, 'memoized', lambda path, since, *a: [
        ('s1', {'id': 'd1', 'completed_at': time.time() - 60, 'title': 'done', 'summary': 'ok'})])
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(host, '_known', {SCOPE: contextvars.copy_context()})
    disk.touch_demand(SCOPE)
    yield seen
    conn.close()
    ksa._reset_cache_for_tests()


def _refresh(source=True):
    return host._event_refresh(SimpleNamespace(replace=lambda *a, **kw: None, control_only=not source))


def _reads(seen):
    return seen.counts, seen.links, seen.checks


def _published():
    return ksa._last_annotation[SCOPE]


def _full_read():
    """What a process with no caches reads now: the answer every gated refresh must equal."""
    saved, known = dict(parts._seen), dict(host._known)
    ksa._reset_cache_for_tests()  # also forgets the holder's scopes and part keys: put those back
    host._known.update(known)
    ksa._annotate_refresh_lock.acquire()
    try:
        return ksa._refresh_annotation_locked(SCOPE)
    finally:
        ksa._annotate_refresh_lock.release()
        parts._seen.update(saved)


def test_a_write_to_one_source_rereads_only_the_parts_that_read_it(world):
    _refresh()
    assert _reads(world) == (1, 1, 1), 'the first read of a scope reads every part'
    first = _published()
    assert first[0] == {'s1': 1} and first[10] == {'s1': 1}

    _refresh()
    assert _reads(world) == (1, 1, 1), 'a wake with no input moved read a part'
    assert _published() == first

    # bookkeeping no reader selects: the board file moves, its content revision does not
    world.conn.execute('CREATE TABLE IF NOT EXISTS kanban_dispatch_stamp (at REAL)')
    _refresh()
    before = _reads(world)
    world.conn.execute('INSERT INTO kanban_dispatch_stamp VALUES (1.0)')
    _refresh()
    assert _reads(world) == before, 'a bookkeeping write reread a part'

    # the plain input (state.db, config): counts and links read it, the done checks do not
    world.plain.write_text('moved')
    _refresh()
    assert _reads(world) == (before[0] + 1, before[1] + 1, before[2])

    # a file only the done checks declared: neither the counts nor the links read it
    world.flagged = False
    world.declared.write_text('moved')
    _refresh()
    assert _reads(world) == (before[0] + 1, before[1] + 1, before[2] + 1)
    assert _published()[10] == {}, 'the done check verdict was not re-evaluated'

    # board content: every part reads the boards
    world.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't1'")
    _refresh()
    assert _reads(world) == (before[0] + 2, before[1] + 2, before[2] + 2)
    assert _published()[0] == {}


def _undated(value):
    """``value`` without the wall times a read stamps on its execution evidence (they date the read itself)."""
    if isinstance(value, dict):
        return {k: _undated(v) for k, v in value.items() if k not in ('observed_at', 'valid_until')}
    if isinstance(value, (tuple, list)):
        return tuple(_undated(v) for v in value)
    return value


def test_every_gated_refresh_publishes_what_a_cold_full_read_would(world):
    steps = [
        lambda: None,
        lambda: world.plain.write_text('one'),
        lambda: world.declared.write_text('one'),
        lambda: world.conn.execute("INSERT INTO tasks VALUES ('t2', 'running', 's2')"),
        lambda: setattr(world, 'flagged', False) or world.declared.write_text('two'),
        lambda: world.conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = 't2'"),
        lambda: world.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't1'"),
        lambda: None,
    ]
    for step in steps:
        step()
        _refresh()
        gated = _published()
        assert _undated(gated) == _undated(_full_read())
        _refresh()  # the cold read above refilled the caches; the gate still answers for the same keys
        assert _undated(_published()) == _undated(gated)


def test_a_due_provider_deadline_rereads_every_part_without_any_write(world, monkeypatch):
    _refresh()
    assert _reads(world) == (1, 1, 1)
    host._clock_deadlines[SCOPE] = [time.time() - 1]  # a wall-time verdict no key sees
    _refresh(source=False)
    assert _reads(world) == (2, 2, 2)
    _refresh()
    assert _reads(world) == (2, 2, 2)


def test_an_undeclared_done_check_or_a_failed_read_is_never_vouched_for(world, monkeypatch):
    _refresh()
    monkeypatch.setattr(host, '_done_event_sources', {})
    _refresh()
    _refresh()
    assert world.checks == 3, 'a done check with no declared inputs was served from its cache'
    assert world.counts == 1

    def boom(scope):
        raise RuntimeError('read failed')
    real = ksa._refresh_annotation_locked
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', boom)
    _refresh()
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', real)
    host._clock_deadlines.pop(SCOPE, None)  # the failed scope's retry deadline; the gate itself is under test
    _refresh()
    assert world.counts == 2, 'a read after a failed one reused parts nothing vouches for'


def test_an_old_counts_read_is_renewed_so_the_published_read_time_stays_adoptable(world, monkeypatch):
    _refresh()
    assert _reads(world) == (1, 1, 1)
    monkeypatch.setattr(host, 'STRICT_REFRESH_AGE_S', 0.0)
    _refresh()
    assert _reads(world) == (2, 1, 1)


def test_the_renewal_reads_this_scopes_counts_age_not_the_last_scope_read(world, monkeypatch):
    _refresh()
    assert world.counts == 1
    monkeypatch.setattr(host, 'STRICT_REFRESH_AGE_S', 5.0)
    with ksa._cache_lock:  # this scope's counts were read a while ago
        ksa._cache_read_at -= 10.0
        ksa._count_scopes.remember_locked()
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('annotation-parts-test', 'other'))
    ksa._all_counts()  # another profile's poll now owns the active count slot, read just now
    assert world.counts == 2
    monkeypatch.setattr(ksa, '_origin_scope', lambda: SCOPE)
    _refresh()
    assert world.counts == 3, "this scope's old counts were not renewed"


def test_deadlines_dropped_while_nobody_polled_force_the_read_when_demand_returns(world, monkeypatch):
    _refresh()
    world.deadline = time.time() + 30  # a done check's declared wall-time boundary
    _refresh()
    interested = host._scope_interested
    monkeypatch.setattr(host, '_scope_interested', lambda scope: False)
    _refresh()  # a source write wakes the holder meanwhile: the scope's deadlines are dropped
    assert SCOPE not in host._clock_deadlines
    world.flagged, world.deadline = False, None  # the verdict moved at a boundary nobody is waiting for now
    monkeypatch.setattr(host, '_scope_interested', interested)
    _refresh(source=False)  # demand is back
    assert _published()[10] == {}, 'a verdict that moved at a declared wall time was served from the cache'


def test_a_declared_board_or_replica_reruns_the_checks_on_content_not_on_bookkeeping(world, monkeypatch, tmp_path):
    """kanban-honest-done declares the boards and their sync replicas. Both files are rewritten several times a
    second by bookkeeping; keyed by stat, every such write reran every done check."""
    shadow = tmp_path / 'sync-shadow'
    shadow.mkdir()
    replica = helpers._board(shadow / 'b.db', [('r1', 'running', 's9')])
    try:
        monkeypatch.setattr(parts, '_replica_dir', lambda: str(shadow))
        check = ksa._done_checks['fixture']
        monkeypatch.setattr(host, '_done_event_sources', {
            'fixture': (check, lambda: [world.board, shadow / 'b.db', world.declared], lambda: None)})
        for conn in (world.conn, replica):
            conn.execute('CREATE TABLE IF NOT EXISTS bookkeeping_stamp (at REAL)')
        _refresh()
        _refresh()
        before = world.checks
        for conn in (world.conn, replica):  # each file moves; no row a reader selects does
            conn.execute('INSERT INTO bookkeeping_stamp VALUES (1.0)')
        _refresh()
        assert world.checks == before, 'a bookkeeping write to a declared board or replica reran the done checks'
        world.flagged = False
        replica.execute("UPDATE tasks SET status = 'done' WHERE id = 'r1'")  # replica content the check may read
        _refresh()
        assert world.checks == before + 1
        assert _published()[10] == {}
    finally:
        replica.close()


def test_a_check_that_offers_a_read_pass_gets_one_around_all_its_cards_and_it_is_closed(world, monkeypatch):
    seen: list[str] = []

    @contextlib.contextmanager
    def one_pass():
        seen.append('open')
        try:
            yield
        finally:
            seen.append('close')

    def check(card):
        seen.append(f"check {card['id']}")
        return True
    check.attention_pass = one_pass
    monkeypatch.setattr(ksa, '_done_checks', {'fixture': check})
    monkeypatch.setattr(host, '_done_event_sources', {})  # undeclared: every refresh runs the checks
    monkeypatch.setattr(ksa._recent_done, 'memoized', lambda path, since, *a: [
        ('s1', {'id': f'd{n}', 'completed_at': time.time() - 60, 'title': 'done', 'summary': 'ok'}) for n in (1, 2)])
    _refresh()
    assert seen == ['open', 'check d1', 'check d2', 'close']
    assert _published()[10] == {'s1': 2}

    def broken():
        raise RuntimeError('no pass today')
    check.attention_pass = broken  # the pass is an optimisation: without it the check still answers
    del seen[:]
    _refresh()
    assert seen == ['check d1', 'check d2']
    assert _published()[10] == {'s1': 2}


def test_a_burst_of_board_writes_is_a_bounded_number_of_reads_and_the_last_write_shows(world, monkeypatch):
    monkeypatch.setattr(host, 'SETTLE_S', (0.25, host.HOLDER_INTERVAL_S))
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([str(world.board), str(world.plain)], []))
    stop = threading.Event()
    with FileEvents() as events:
        thread = threading.Thread(
            target=lambda: run(events, stop, SimpleNamespace(held=lambda: True, beat=lambda: None),
                               lambda: host._event_refresh(events)), name='kanban-annotation-holder')
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while world.counts < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.wait(max(host.SETTLE_S) + 0.5)  # the ownership read's confirmations
            base = _reads(world)
            writes = 40
            started = time.monotonic()
            for n in range(writes):
                world.conn.execute("INSERT INTO tasks VALUES (?, 'running', 's1')", (f'burst-{n}',))
                time.sleep(0.01)  # health: allow HX013 -- the spacing of the burst is the input under test
            burst = time.monotonic() - started
            deadline = time.monotonic() + 5
            while ksa._last_annotation[SCOPE][0] != {'s1': writes + 1} and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ksa._last_annotation[SCOPE][0] == {'s1': writes + 1}, 'the last write of the burst is not shown'
            stop.wait(max(host.SETTLE_S) + 0.5)  # the trailing confirmations
            reads = world.counts - base[0]
            # one read at once, one per coalescing gap while the burst lasts, one trailing
            assert reads <= 2 + burst / 0.25 + 1, (reads, burst)
            assert reads < writes / 4
            quiet = _reads(world)
            stop.wait(1.0)
            assert _reads(world) == quiet, 'a settled burst kept reading'
        finally:
            stop.set()
            events.wake()
            thread.join(5)
    assert not thread.is_alive()


class _Pass:
    """Not a generator: nothing but an explicit exit closes it."""

    def __init__(self, seen):
        self.seen = seen

    def __enter__(self):
        self.seen.append('open')

    def __exit__(self, *exc):
        self.seen.append('close')
        return False


def _check_with_pass(monkeypatch, seen, factory=None):
    def check(card):
        seen.append(f"check {card['id']}")
        return True
    check.attention_pass = factory or (lambda: _Pass(seen))
    monkeypatch.setattr(ksa, '_done_checks', {'fixture': check})
    monkeypatch.setattr(host, '_done_event_sources', {})


def test_a_pass_that_is_not_a_generator_is_closed_with_the_count(world, monkeypatch):
    seen: list[str] = []
    _check_with_pass(monkeypatch, seen)
    _refresh()
    assert seen == ['open', 'check d1', 'close']


def test_a_pass_is_closed_when_the_count_raises(world, monkeypatch):
    seen: list[str] = []
    _check_with_pass(monkeypatch, seen)

    def boom(path, since, *a):
        raise RuntimeError('unexpected')
    monkeypatch.setattr(ksa._recent_done, 'memoized', boom)
    _refresh()
    assert seen == ['open', 'close']


def test_a_pass_whose_exit_raises_keeps_the_verdicts_and_closes_the_other_passes(world, monkeypatch):
    seen: list[str] = []

    @contextlib.contextmanager
    def failing():
        try:
            yield
        finally:
            raise RuntimeError('close failed')

    def other(card):
        return False
    other.attention_pass = lambda: _Pass(seen)
    _check_with_pass(monkeypatch, seen, failing)
    monkeypatch.setattr(ksa, '_done_checks', {'other': other, **ksa._done_checks})
    monkeypatch.setattr(ksa._recent_done, 'memoized', lambda path, since, *a: [
        ('s1', {'id': f'd{n}', 'completed_at': time.time() - 60, 'title': 'done', 'summary': 'ok'}) for n in (1, 2)])
    _refresh()
    assert seen == ['open', 'check d1', 'check d2', 'close']
    assert _published()[10] == {'s1': 2}, 'a failing close lost the verdicts the checks returned'


def test_a_replica_whose_revision_cannot_vouch_reruns_the_checks(world, monkeypatch, tmp_path):
    shadow = tmp_path / 'sync-shadow'
    shadow.mkdir()
    replica = sqlite3.connect(shadow / 'b.db', isolation_level=None)  # no revision triggers: stat fallback
    replica.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)")
    replica.execute("INSERT INTO tasks VALUES ('r1', 'running', 's9')")
    try:
        monkeypatch.setattr(parts, '_replica_dir', lambda: str(shadow))
        check = ksa._done_checks['fixture']
        monkeypatch.setattr(host, '_done_event_sources', {
            'fixture': (check, lambda: [world.board, shadow / 'b.db'], lambda: None)})
        assert ksa._board_content_key(shadow / 'b.db', ksa._revision.SHADOW_TABLES)[:1] == ('stat',)
        _refresh()
        before = world.checks
        _refresh()  # written < STAT_SETTLED_S ago: the stat vouches for nothing, so the checks rerun
        assert world.checks == before + 1, 'an unsettled stat-keyed replica was trusted'
    finally:
        replica.close()


def test_a_scope_whose_counts_sit_in_a_saved_slot_is_not_renewed_before_its_age(world, monkeypatch):
    _refresh()
    assert world.counts == 1
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('annotation-parts-test', 'other'))
    ksa._all_counts()  # another profile's poll owns the active slot
    assert world.counts == 2
    monkeypatch.setattr(ksa, '_origin_scope', lambda: SCOPE)
    _refresh()
    assert world.counts == 2, 'fresh counts in a saved slot were read again'
