"""The roller's idle-fenced backstop honors the connection drain window."""
import subprocess

import pytest

from hermes_cli import always_on_rollover as ro
from hermes_cli.web_server_skew_exit import DRAIN_CAP_S
from tests.hermes_cli.test_always_on_rollover import FakeHost, FakeFence, make_reaper
from tests.hermes_cli import test_always_on_rollover as helpers

home = helpers.home


@pytest.mark.parametrize('demoted', [False, True])
def test_idle_backend_stopped_by_roller_at_drain_cap(home, demoted):
    host, fence, wall = FakeHost(running={9148, 9149}), FakeFence(), [1000.0]
    ro.write_active(9149)
    roller = make_reaper(home, host, fence, wall)
    if demoted:
        roller.demote(9148)
    assert roller.reap_superseded() == []
    wall[0] += DRAIN_CAP_S - 1
    assert roller.reap_superseded() == []
    assert not fence.posts
    wall[0] += 1
    assert roller.reap_superseded() == [9148]
    assert (9148, 'prepare') in fence.posts
    assert (9148, 'commit') in fence.posts
    assert ('stop', '--no-block', ro.backend_unit(9148)) in host.calls
    assert not any(ro.backend_unit(9149) in call and call[0] == 'stop' for call in host.calls)


def test_busy_turn_is_preserved_at_drain_cap_then_stopped_when_idle(home):
    host, fence, wall = FakeHost(running={9148, 9149}), FakeFence(busy={9148}), [1000.0]
    ro.write_active(9149)
    roller = make_reaper(home, host, fence, wall)
    assert roller.reap_superseded() == []
    wall[0] += DRAIN_CAP_S
    assert roller.reap_superseded() == []
    assert (9148, 'prepare') in fence.posts
    assert not any(c[0] == 'stop' for c in host.calls)
    fence.busy.clear()
    assert roller.reap_superseded() == [9148]


def test_failed_stop_keeps_original_drain_age_for_retry(home):
    host, fence, wall = FakeHost(running={9148, 9149}), FakeFence(), [1000.0]
    ro.write_active(9149)
    roller = make_reaper(home, host, fence, wall)
    assert roller.reap_superseded() == []
    real = roller.systemctl
    def fail(*args, **kwargs):
        if args[0] == 'stop':
            return subprocess.CompletedProcess(args, 1, '', 'failed')
        return real(*args, **kwargs)
    roller.systemctl = fail
    wall[0] += DRAIN_CAP_S
    assert roller.reap_superseded() == []
    assert roller._load_superseded()[9148] == 1000.0
    roller.systemctl = real
    assert roller.reap_superseded() == [9148]
