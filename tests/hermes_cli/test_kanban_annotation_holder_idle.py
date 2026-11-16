"""Idle holder: unchanged inputs cost no rebuild and no per-scope pass; the holder wakes on a change, a kick or its
>= 60 s due-time fallback, not on a fixed 2 s / 12 s cadence (t_01a1db1387f61a98 phase 2, owner decision A). [daveey]

Deployed P1 (run3, 600 s): ``lease.kanban-annotation`` 11.55% of a core, ``annotation.refresh`` 10.88% over 294 runs,
and every scope's ``annotation.holder_refresh`` ran 247 times (every ~2.4 s) while only ~30 of them did anything.
"""
import contextvars
import itertools
import time

import pytest

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_host as helpers

world = helpers.world
SCOPE = helpers.SCOPE


@pytest.fixture
def held(world, monkeypatch):
    """A holder that has published and verified ``SCOPE`` once; counts the work every later pass does."""
    counts = {"narrow": 0, "load": 0, "wakes": 0}
    narrow_now, load_host, refresh = host._narrow_now, disk.load_host, host._holder_refresh

    def narrow(*a, **k):
        counts["narrow"] += 1
        return narrow_now(*a, **k)

    def load(*a, **k):
        counts["load"] += 1
        return load_host(*a, **k)

    def wake(*a, **k):
        counts["wakes"] += 1
        return refresh(*a, **k)

    monkeypatch.setattr(host, "_narrow_now", narrow)
    monkeypatch.setattr(disk, "load_host", load)
    monkeypatch.setattr(host, "_holder_refresh", wake)
    host._known[SCOPE] = contextvars.copy_context()
    disk.touch_demand(SCOPE)
    assert host._holder_pass() == 1
    # Past the floor the holder verifies its own read once; from then on nothing it watches moves.
    for _ in range(int(host.HOLDER_MOVED_FLOOR_S / host.HOLDER_INTERVAL_S) + 1):
        _tick(world)
    assert world["calls"]["recompute"] == 1
    for key in counts:
        counts[key] = 0
    return counts


def _tick(world, n: int = 1) -> int:
    refreshed = 0
    for _ in range(n):
        helpers._advance(world, host.HOLDER_INTERVAL_S)
        disk.touch_demand(SCOPE)  # a display keeps polling the scope
        refreshed += host._holder_pass()
    return refreshed


def test_idle_passes_do_no_rebuild_and_no_scope_work(world, held):
    """RED on the fixed cadence: the 20 s renewal rebuilt, and every post-floor pass loaded the copy and probed."""
    ticks = int(40 / host.HOLDER_INTERVAL_S)  # 40 more s of steady demand (to ~52 s after the read), inputs unchanged
    assert _tick(world, ticks) == 0
    assert world["calls"]["recompute"] == 1  # no rebuild
    assert held == {"narrow": 0, "load": 0, "wakes": 0}  # no per-scope pass, no copy read, no narrow key


def test_idle_cpu_is_bounded(world, held):
    start = time.process_time()
    _tick(world, 200)
    # 200 idle passes over one scope: a signature (a few stat/read syscalls) each; a narrow key and a read only at
    # the fallback renewals (the renewal's own key, then one verification past its floor).
    assert time.process_time() - start < 1.0
    assert held["narrow"] <= 2 * (200 * host.HOLDER_INTERVAL_S / host.HOLDER_REFRESH_AGE_S + 1)
    assert world["calls"]["recompute"] <= 1 + 200 * host.HOLDER_INTERVAL_S / host.HOLDER_REFRESH_AGE_S + 1


def test_due_time_fallback_is_at_least_sixty_seconds(world, held):
    """With nothing moving, the scope wakes only at its due-time fallback (a real re-read, >= 60 s apart)."""
    assert host.HOLDER_REFRESH_AGE_S >= 60.0
    renewals = []
    for i in range(int(180 / host.HOLDER_INTERVAL_S)):
        if _tick(world):
            renewals.append(world["wall"])
    assert len(renewals) >= 2, renewals
    gaps = [b - a for a, b in itertools.pairwise(renewals)]
    assert min(gaps) >= 60.0, gaps
    assert max(gaps) <= host.HOLDER_REFRESH_AGE_S + host.HOLDER_MOVED_FLOOR_S + 2 * host.HOLDER_INTERVAL_S, gaps
    # Each renewal wakes the scope a few times (read, floor, verification), not every 2 s.
    assert held["wakes"] <= 4 * (len(renewals) + 1), held


def test_a_board_write_wakes_the_scope_next_tick(world, held):
    world["conn"].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    _tick(world)
    assert held["wakes"] == 1 and held["narrow"] >= 1  # woken by the change, probed it


def test_a_liveness_only_write_wakes_but_does_not_rebuild(world, held, monkeypatch):
    monkeypatch.setattr(host, "_narrow_now", lambda scope, full: _key(scope))  # the published key: liveness only
    world["conn"].execute("UPDATE tasks SET status = status WHERE id = 't1'")
    assert _tick(world, 5) == 0
    assert world["calls"]["recompute"] == 1


def _key(scope):
    out: dict = {}
    disk.load_host(scope, shared=True, header_out=out)
    return out.get("narrow_key")


def test_a_substantive_write_refreshes_after_the_floor(world, held):
    world["conn"].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    refreshed = _tick(world, int(host.HOLDER_MOVED_FLOOR_S / host.HOLDER_INTERVAL_S) + 1)
    assert refreshed == 1
    assert ksa._last_annotation[SCOPE][0]["s1"] == 3


def test_own_kick_wakes_at_once(world, held):
    host.kick(SCOPE)
    assert _tick(world) == 1


def test_foreign_cue_wakes(world, held):
    from hermes_cli import kanban_annotation_mutation as mutation

    mutation.record(SCOPE)  # as another process does: only the cue file changes
    assert _tick(world) == 1


def test_quiet_watched_copy_stays_displayable_without_header_rewrites(world, held):
    """Nothing the holder watches moved since it verified: a display adopts the copy past the old 12 s bound
    with no confirmation rewrite in between."""
    _tick(world, int(40 / host.HOLDER_INTERVAL_S))
    helpers._other_process()
    assert host.adopt_display(SCOPE) is not None


def test_moved_unverified_copy_still_ages_out_for_display(world, held, monkeypatch):
    monkeypatch.setattr(host, "_holder_pass", lambda: 0)  # the holder is gone
    world["conn"].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    helpers._advance(world, host.DISPLAY_MAX_AGE_S + 1)
    helpers._other_process()
    assert host.adopt_display(SCOPE) is None


def test_quiet_copy_ages_out_past_the_watch_bound(world, held):
    helpers._advance(world, host.DISPLAY_WATCH_MAX_AGE_S + 1)
    helpers._other_process()
    assert host.adopt_display(SCOPE) is None


def test_poll_published_copy_keeps_the_twelve_second_display_bound(world):
    helpers._poll()  # a poll's copy carries no narrow key and no watch signature
    helpers._advance(world, host.DISPLAY_MAX_AGE_S + 1)
    helpers._other_process()
    assert host.adopt_display(SCOPE) is None


def test_unreadable_watch_inputs_wake_the_scope(world, held, monkeypatch):
    monkeypatch.setattr(host, "_watch_paths", lambda scope: (_ for _ in ()).throw(OSError("gone")), raising=False)
    _tick(world)
    assert held["wakes"] == 1  # fail closed: an unknown signature is a change


def test_exact_callers_keep_the_twenty_second_renewal(world, held):
    """An exact (strict) caller adopts only copies younger than ``_COUNTS_MAX_AGE_S``; while one polls the scope,
    the holder renews every ``STRICT_REFRESH_AGE_S`` so the caller adopts instead of reading on the poll path."""
    assert host.STRICT_REFRESH_AGE_S + host.HOLDER_INTERVAL_S < ksa._COUNTS_MAX_AGE_S
    renewals = []
    for _ in range(int(90 / host.HOLDER_INTERVAL_S)):
        disk.touch_demand(SCOPE, strict=True)  # e.g. the sessions API polling
        if _tick(world):
            renewals.append(world["wall"])
    gaps = [b - a for a, b in itertools.pairwise(renewals)]
    assert len(renewals) >= 4 and max(gaps) <= host.STRICT_REFRESH_AGE_S + host.HOLDER_INTERVAL_S, renewals
    assert held["wakes"] <= 3 * (len(renewals) + 1), held  # still no 2 s cadence in between


def test_strict_demand_ages_out_to_the_sixty_second_fallback(world, held):
    disk.touch_demand(SCOPE, strict=True)
    helpers._advance(world, host.DEMAND_WINDOW_S + 1)
    assert host._renew_age(SCOPE) == host.HOLDER_REFRESH_AGE_S
    disk.touch_demand(SCOPE, strict=True)
    assert host._renew_age(SCOPE) == host.STRICT_REFRESH_AGE_S


def test_display_polls_do_not_mark_strict_demand(world):
    ksa.annotate_kanban_activity([{"id": "s1"}], display=True)
    assert disk.demand_age(SCOPE) is not None
    assert disk.demand_age(SCOPE, strict=True) is None
    helpers._poll()  # an exact caller
    assert disk.demand_age(SCOPE, strict=True) is not None
