"""Served-home membership seeding for the production ``hermes_cli.local_work.Registry``.

The process profile reservation (``gateway.runtime_ownership.process_ownership``) is the authority on
which homes a gateway serves; it seeds registry membership around every lock acquisition and release.
Every test drives the real Registry (no stand-in) and real ``gateway.lock`` files under tmp homes.
RED on 95089d00fb: nothing seeded membership, so the first reservation latched sticky ``unknown-home``,
and the real Registry had no ``membership_generation``/``coverage_pending``.
"""
from __future__ import annotations

import os
import types

import pytest

from agent import local_work_agent
from gateway import quiet_control, runtime_ownership
from gateway.runtime_ownership import OwnershipConflict, ProfileOwnership, exclusive_maintenance
from hermes_cli import local_work
from hermes_constants import hermes_home_key


@pytest.fixture
def registry(monkeypatch):
    reg = local_work.Registry()  # exactly what hermes_bootstrap installs: homes=()
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


@pytest.fixture
def homes(tmp_path):
    real = tmp_path / "real-home"
    real.mkdir(mode=0o700)
    link = tmp_path / "linked-home"
    link.symlink_to(real, target_is_directory=True)
    return real, link


def test_process_ownership_singleton_tracks_membership():
    assert runtime_ownership.process_ownership._track_membership is True
    assert ProfileOwnership()._track_membership is False  # maintenance/ad-hoc owners never seed


def test_reservation_seeds_canonical_membership_for_every_home_spelling(registry, homes, monkeypatch):
    real, link = homes
    owner = ProfileOwnership(track_membership=True)
    assert "membership_unseeded" in registry.coverage_pending()
    owner.reserve([link])  # a non-canonical (symlinked) spelling of the served home
    try:
        snap = registry.snapshot()
        key = local_work.canonical_home(real)
        assert snap.membership == {key: ("ready", ("owned",))}
        assert snap.pending_transitions == 0 and registry.coverage_pending() == []
        assert key == hermes_home_key(link) == hermes_home_key(real)
        # Every caller spelling of the home is the member: raw symlink, resolved path, hermes_home_key,
        # str(get_hermes_home()) (agent seam default) under a symlinked HERMES_HOME.
        monkeypatch.setenv("HERMES_HOME", str(link))
        for spelling in (str(link), str(real), hermes_home_key(link), os.path.join(str(link), ".")):
            registry.finish(registry.reserve("probe", home=spelling, source="test"))
        local_work_agent.release(local_work_agent.reserve("agent_turn", "test.default_home"))
        snap = registry.snapshot()
        assert snap.unknowns == () and snap.active == 0
    finally:
        owner.close()


def test_release_retires_home_and_late_reentry_invalidates(registry, homes):
    real, _link = homes
    owner = ProfileOwnership(track_membership=True)
    owner.reserve([real])
    generation = registry.membership_generation()
    owner.release(real)
    snap = registry.snapshot()
    assert local_work.canonical_home(real) not in snap.membership
    assert snap.membership_generation > generation and snap.pending_transitions == 0
    assert snap.unknowns == ()
    late = registry.reserve("late-callback", home=str(real), source="test")
    assert "retired-home-reentry" in registry.snapshot().unknowns
    registry.finish(late)


def test_failed_reservation_leaves_no_member_and_no_open_transition(registry, homes):
    real, _link = homes
    holder = ProfileOwnership()  # another owner (separate open file description) holds the flock
    holder.reserve([real])
    try:
        owner = ProfileOwnership(track_membership=True)
        with pytest.raises(OwnershipConflict):
            owner.reserve([real])
        snap = registry.snapshot()
        assert snap.pending_transitions == 0
        assert local_work.canonical_home(real) not in snap.membership
        assert "membership_unseeded" in registry.coverage_pending()
    finally:
        holder.close()


def test_maintenance_reservation_never_touches_membership(registry, homes):
    real, _link = homes
    before = registry.membership_generation()
    with exclusive_maintenance([real], purpose="test"):
        assert registry.snapshot().membership == {}
    assert registry.membership_generation() == before


def test_first_reservation_after_launch_lock_is_not_unknown_home(registry, homes, monkeypatch):
    """Behavioral RED on 95089d00fb (existing APIs only): boot reserves the launch home, then the first
    agent reservation latched sticky ``unknown-home`` because nothing seeded membership."""
    from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock

    _real, link = homes
    monkeypatch.setenv("HERMES_HOME", str(link))
    assert acquire_gateway_runtime_lock()
    try:
        local_work_agent.release(local_work_agent.reserve("agent_turn", "test.first"))
        assert registry.snapshot().unknowns == ()
    finally:
        release_gateway_runtime_lock()


def test_launch_lock_acquisition_seeds_the_installed_registry(registry, homes, monkeypatch):
    """Production boot path: ``acquire_gateway_runtime_lock`` reserves the launch home through the
    tracked singleton, which seeds the bootstrap-installed registry."""
    from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock

    real, link = homes
    monkeypatch.setenv("HERMES_HOME", str(link))
    assert acquire_gateway_runtime_lock()
    try:
        assert registry.membership_snapshot() == {local_work.canonical_home(real): ("ready", ("owned",))}
        local_work_agent.release(local_work_agent.reserve("agent_turn", "test.launch"))
        assert registry.snapshot().unknowns == ()
    finally:
        release_gateway_runtime_lock()
    assert registry.membership_snapshot() == {}


def test_quiet_control_pending_reads_the_real_registry(registry, homes):
    real, _link = homes
    runner = types.SimpleNamespace(_quiet_registry=registry, _quiet_bootstrap_provenance=True,
                                   _quiet_attested=True, _quiet_code=types.SimpleNamespace(
                                       unknowns=tuple, latched_unknowns=tuple))
    control = quiet_control.QuietControl(runner)
    pending = control._pending(registry)
    assert "membership_unsupported" not in pending and "membership_unseeded" in pending
    owner = ProfileOwnership(track_membership=True)
    owner.reserve([real])
    try:
        assert control._pending(registry) == []
        view, _ = control._registry_view(registry)
        assert view["membership_generation"] == registry.membership_generation()
    finally:
        owner.close()
