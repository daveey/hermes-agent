"""Retirement deadlines survive failed requests and indeterminate unit state."""
import subprocess

import pytest

from hermes_cli import always_on_rollover as ro
from tests.hermes_cli import test_always_on_rollover as helpers

home = helpers.home


@pytest.mark.parametrize('demoted', [False, True])
@pytest.mark.parametrize('failure', ['nonzero', 'oserror', 'timeout'])
def test_hard_stop_failure_preserves_original_ages_and_retries(home, demoted, failure):
    host, fence, wall = helpers.FakeHost(running={9148, 9149}), helpers.FakeFence(busy={9148}), [1000.0]
    ro.write_active(9149)
    roller = helpers.make_reaper(home, host, fence, wall)
    if demoted:
        roller.demote(9148)
    assert roller.reap_superseded() == []
    since, demotions = roller._load_superseded(), roller._load_demoted()
    wall[0] += ro.DEMOTED_HARD_S if demoted else ro.SUPERSEDED_HARD_S
    original = host.systemctl
    stops = []

    def refused(*args, **kwargs):
        if args[0] == 'stop':
            stops.append(args)
            if failure == 'oserror':
                raise OSError('manager unavailable')
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(args, 15)
            return subprocess.CompletedProcess(args, 1, '', 'refused')
        return original(*args, **kwargs)

    host.systemctl = refused
    assert helpers.make_reaper(home, host, fence, wall).reap_superseded() == []
    assert roller._load_superseded() == since
    assert roller._load_demoted() == demotions
    assert len(stops) == 1
    wall[0] += ro.SUPERVISE_POLL_S
    host.systemctl = original
    assert helpers.make_reaper(home, host, fence, wall).reap_superseded() == [9148]
    assert host.running == {9149}


@pytest.mark.parametrize('state', ['deactivating', 'unknown', 'oserror', 'timeout'])
def test_reaping_does_not_forget_age_until_confirmed_inactive(home, state):
    host, fence, wall = helpers.FakeHost(running={9148, 9149}), helpers.FakeFence(busy={9148}), [1000.0]
    ro.write_active(9149)
    roller = helpers.make_reaper(home, host, fence, wall)
    roller.demote(9148)
    assert roller.reap_superseded() == []
    since, demotions = roller._load_superseded(), roller._load_demoted()
    original = host.systemctl

    def querying(*args, **kwargs):
        if args[0] == 'is-active' and ro.backend_unit(9148) in args:
            if state == 'oserror':
                raise OSError('manager unavailable')
            if state == 'timeout':
                raise subprocess.TimeoutExpired(args, 15)
            return subprocess.CompletedProcess(args, 3, state, '')
        return original(*args, **kwargs)

    host.systemctl = querying
    wall[0] += ro.DEMOTED_HARD_S
    assert helpers.make_reaper(home, host, fence, wall).reap_superseded() == []
    assert roller._load_superseded() == since
    assert roller._load_demoted() == demotions
    host.systemctl = original
    assert helpers.make_reaper(home, host, fence, wall).reap_superseded() == [9148]
    assert helpers.make_reaper(home, host, fence, wall).reap_superseded() == []
    assert roller._load_superseded() == roller._load_demoted() == {}


@pytest.mark.parametrize('demoted', [False, True])
def test_accepted_async_stop_retains_age_while_unit_is_still_active(home, demoted):
    host, fence, wall = helpers.FakeHost(running={9148, 9149}), helpers.FakeFence(busy={9148}), [1000.0]
    ro.write_active(9149)
    roller = helpers.make_reaper(home, host, fence, wall)
    if demoted:
        roller.demote(9148)
    roller.reap_superseded()
    since, demotions = roller._load_superseded(), roller._load_demoted()
    original = host.systemctl
    attempts = []

    def accepted(*args, **kwargs):
        if args[0] == 'stop':
            attempts.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')
        return original(*args, **kwargs)

    host.systemctl = accepted
    wall[0] += ro.DEMOTED_HARD_S if demoted else ro.SUPERSEDED_HARD_S
    assert helpers.make_reaper(home, host, fence, wall).reap_superseded() == [9148]
    assert roller._load_superseded() == since
    assert roller._load_demoted() == demotions
    wall[0] += ro.SUPERVISE_POLL_S
    assert helpers.make_reaper(home, host, fence, wall).reap_superseded() == [9148]
    assert len(attempts) == 2
    host.running.discard(9148)
    assert roller.reap_superseded() == []
    assert roller._load_superseded() == roller._load_demoted() == {}
