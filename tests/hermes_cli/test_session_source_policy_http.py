"""HTTP payload differential against the pre-reuse source-policy algorithm."""

import pytest

from hermes_cli import session_listing as listing
from tests.hermes_cli import test_profiles_sidebar_scope as fixtures

_uncached_sidebar_endpoints = fixtures._uncached_sidebar_endpoints
client = fixtures.client
profiles_on_disk = fixtures.profiles_on_disk
_seed_session = fixtures._seed_session


def _legacy_policy(home):
    policy = listing.AutomatedSourcePolicy(
        listing._sessions_source_list(home, "automated_sources"),
        listing._sessions_source_list(home, "interactive_sources"))
    profile, root = listing._store_profile_and_root(home)
    sidebar_profiles = listing._sessions_source_list(root, "sidebar_profiles")
    if (sidebar_profiles and profile.lower() not in sidebar_profiles
            and not listing._profile_sets_source_policy(home)):
        return listing.AutomatedSourcePolicy(["*"])
    return policy


@pytest.mark.parametrize("route", ["/api/profiles/sessions", "/api/profiles/projects/tree"])
@pytest.mark.parametrize("automated", ["include", "exclude"])
def test_http_payload_identical_to_legacy(client, profiles_on_disk, monkeypatch, route, automated):
    from hermes_cli.web_routers import profiles as routes
    from hermes_cli import kanban_session_activity as activity
    from unittest.mock import Mock
    import time

    # Keep the observation producer's clock identical, not a stripped/normalized payload.
    clock = Mock(wraps=time)
    clock.time.return_value = time.time() - 60
    monkeypatch.setattr(activity, "time", clock)

    for name, home in profiles_on_disk.items():
        (home / "config.yaml").write_text(
            "sessions:\n  interactive_sources: [desktop, cli]\n")
        _seed_session(home, f"{name}-human", source="cli")
        _seed_session(home, f"{name}-bot", source="eval")
    modern = listing.automated_session_sources
    with monkeypatch.context() as patch:
        patch.setattr(listing, "automated_session_sources", _legacy_policy)
        patch.setattr(routes, "automated_session_sources", _legacy_policy)
        expected = client.get(route, params={"automated": automated})
    monkeypatch.setattr(routes, "automated_session_sources", modern)
    actual = client.get(route, params={"automated": automated})
    assert expected.status_code == actual.status_code == 200
    assert expected.json()["errors"] == []
    assert actual.json() == expected.json()
    assert actual.content == expected.content
