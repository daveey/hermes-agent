"""One process reading the hub replica for several origin profiles keeps a projection state per profile. [daveey]

A Desktop ``serve`` answers every profile of its home: the ``profile=all`` session polls and the annotation
holder's scopes each read the replica under another active profile. With one state per replica, each profile's read
discarded the previous profile's state and decoded every row again (18,264 rows, ~1 s of CPU per read on the mini,
five reads per 2 s holder pass: t_01a1d6d42877f719). Each case alternates profiles over a 12k-row replica, compares
every live read with the full (explicit ``now``) read of the same profile, and pins the decode counters; nothing
here measures time.
"""
import json

import pytest

from hermes_cli import kanban_replica_projection as projection
from hermes_cli import kanban_replica_rows as replica_rows
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_replica_incremental as _incremental
from tests.hermes_cli.test_kanban_replica_read_memo import card

hub = _incremental.hub
world = _incremental.world
N = _incremental.N
PROFILES = ('default', 'bb-dev', 'bb-replay')


@pytest.fixture
def as_profile(monkeypatch):
    """Switch the origin profile both readers see (``_read_remote_links`` and ``project`` import it at call time)."""
    def switch(name):
        monkeypatch.setattr('hermes_cli.profiles.get_active_profile_name', lambda: name)
    return switch


def _linking(tid, profile, session):
    t = card(tid)
    t['sessions'] = [{'gw': 'origin', 'profile': profile, 'session': session, 'rel': 'shepherd'}]
    return json.dumps(t)


def _zero():
    replica_rows.stats.update(dict.fromkeys(replica_rows.stats, 0))
    projection.stats.update(dict.fromkeys(projection.stats, 0))


def _read(w, as_profile, name):
    """The live read as ``name``, checked against the full read as ``name``."""
    as_profile(name)
    live, full, live_outs, full_outs = w.both()
    assert live == full and live_outs == full_outs
    return live


def _link_each_profile(w):
    for n, name in enumerate(PROFILES):
        w.put(f'work/build/t_p{n}', _linking(f't_p{n}', name, f's_{name}'))
    w.tick(1001.0)


def test_alternating_profiles_decode_each_row_once_and_then_only_the_changed_row(world, as_profile):
    w, _ = world
    _link_each_profile(w)
    _zero()
    first = {name: _read(w, as_profile, name) for name in PROFILES}
    # ``default`` decodes all N + 3 rows and keeps whole the N/4 + 1 that link it. ``bb-dev`` decodes those again
    # (slim, now shared) and its own t_p1; ``bb-replay`` decodes its own t_p2 and t_p1, which ``bb-dev`` keeps whole.
    own = N // 4 + 1
    assert projection.stats['projected'] == (N + 3) + (own + 1) + 2
    assert projection.stats['shared'] == 2 * (N + 3) - (own + 1) - 2
    for name in PROFILES:
        assert f's_{name}' in json.dumps(first[name])
        assert all(f's_{other}' not in json.dumps(first[name]) for other in PROFILES if other != name)

    for round_ in range(3):  # the holder's pass: one replica change, then every profile reads
        w.put('work/build/t_7', json.dumps(_incremental._task(7, title=f'moved {round_}')))
        w.tick(1002.0 + round_)
        _zero()
        for name in PROFILES:
            assert _read(w, as_profile, name) == first[name]
        assert projection.stats['projected'] == 1  # the changed row, once, for all three profiles
        assert projection.stats['shared'] == len(PROFILES) - 1
        assert replica_rows.stats['full'] == 0 and replica_rows.stats['delta_rows'] == 1


def test_an_unchanged_replica_is_answered_from_each_profiles_memo(world, as_profile):
    w, _ = world
    _link_each_profile(w)
    for name in PROFILES:
        _read(w, as_profile, name)
    _zero()
    for _ in range(2):
        for name in PROFILES:
            as_profile(name)
            activity._read_remote_links(w.path)
    assert projection.stats == dict.fromkeys(projection.stats, 0)
    assert replica_rows.stats == dict.fromkeys(replica_rows.stats, 0)


def test_a_row_that_starts_and_stops_linking_one_profile_shows_only_there(world, as_profile):
    w, _ = world
    before = {name: _read(w, as_profile, name) for name in PROFILES}
    w.put('work/build/t_9', _linking('t_9', 'bb-dev', 's_dev'))
    w.tick(1001.0)
    during = {name: _read(w, as_profile, name) for name in PROFILES}
    assert during['default'] == before['default'] and during['bb-replay'] == before['bb-replay']
    assert len(during['bb-dev']) == len(before['bb-dev']) + 1
    w.put('work/build/t_9', _linking('t_9', 'bb-replay', 's_replay'))
    w.tick(1002.0)
    after = {name: _read(w, as_profile, name) for name in PROFILES}
    assert after['default'] == before['default'] and after['bb-dev'] == before['bb-dev']
    assert len(after['bb-replay']) == len(before['bb-replay']) + 1
    w.delete('work/build/t_9')
    w.tick(1003.0)
    assert {name: _read(w, as_profile, name) for name in PROFILES} == before


def test_a_malformed_row_fails_every_profile_and_recovers_for_every_profile(world, as_profile):
    w, _ = world
    before = {name: _read(w, as_profile, name) for name in PROFILES}
    w.put('work/build/t_11', '{not json')
    w.tick(1001.0)
    for name in PROFILES:
        as_profile(name)
        with pytest.raises(ValueError):
            activity._read_remote_links(w.path)
    w.put('work/build/t_11', json.dumps(_incremental._task(11)))
    w.tick(1002.0)
    assert {name: _read(w, as_profile, name) for name in PROFILES} == before


def test_states_and_shared_stages_are_bounded_and_leave_with_their_replica(world, as_profile):
    w, _ = world
    for i in range(projection._MAX_STATES + 4):
        as_profile(f'p{i}')
        activity._read_remote_links(w.path)
    assert len(projection._states) == projection._MAX_STATES
    assert len(activity._remote_read_memo) <= activity._REMOTE_READ_MEMOS
    assert set(projection._shared) == {str(w.path)} and len(projection._shared[str(w.path)]) == N
    w.delete('work/build/t_3')
    w.tick(1001.0)
    activity._read_remote_links(w.path)
    assert ('owner', 'work/build/t_3') not in projection._shared[str(w.path)]
    with projection._lock:
        projection._states.clear()
        projection._forget_unused_shared()
    assert projection._shared == {}


def test_deleted_shared_rows_leave_even_when_only_new_scopes_read(world, as_profile):
    w, _ = world
    for i in range(projection._MAX_STATES + 2):
        as_profile(f'new{i}')
        activity._read_remote_links(w.path)
        assert len(projection._shared[str(w.path)]) == N - i
        w.delete(f'work/build/t_{i}')
        w.tick(1001.0 + i)
