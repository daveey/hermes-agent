"""Browser deadlines survive queuing/reconnect without becoming process leases."""
import pytest


def test_browser_durations_never_renew_from_transport_or_delayed_delivery():
    from hermes_cli import serve_producer_leases as leases

    original = leases.BrowserInterest.accepted('boot-a', now=10, visible=True)
    assert original.visibility_until == 85
    assert original.watch_until == 910
    for now in (40, 70, 84):
        assert original.remaining('boot-a', now=now) == (85 - now, 910 - now)
    assert original.remaining('boot-a', now=85) == (0, 825)
    assert original.remaining('boot-a', now=909) == (0, 1)
    assert original.remaining('boot-a', now=910) == (0, 0)
    assert original.remaining('boot-b', now=10) == (0, 0)
    # A transport retainer does not modify the independently accepted browser lease.
    retainer = leases.Retainer('boot-a', now=10)
    retainer.renew('boot-a', now=14)
    assert retainer.live('boot-a', now=28)
    assert not retainer.live('boot-a', now=29)
    assert original.remaining('boot-a', now=910) == (0, 0)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, True, '1', None])
def test_invalid_clock_cannot_create_or_renew_a_lease(bad):
    from hermes_cli import serve_producer_leases as leases

    with pytest.raises(ValueError, match='clock'):
        leases.BrowserInterest.accepted('boot-a', now=bad, visible=True)
