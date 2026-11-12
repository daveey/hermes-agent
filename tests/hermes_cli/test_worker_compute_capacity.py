"""Worker capacity follows live cgroups, not just running card rows."""
import pytest


@pytest.mark.parametrize(('held', 'load', 'expected'), [(False, 23.9, False), (False, 24, True),
                                                      (True, 20, True), (True, 16, False)])
def test_load_brake_thresholds(held, load, expected):
    from hermes_cli.worker_compute_capacity import pressure_held
    assert pressure_held(held, load, 16) is expected


@pytest.mark.parametrize(('load', 'cores'), [(float('nan'), 16), (float('inf'), 16), (-1, 16), (1, 0)])
def test_invalid_pressure_is_not_free_capacity(load, cores):
    from hermes_cli.worker_compute_capacity import pressure_held
    with pytest.raises(ValueError):
        pressure_held(False, load, cores)


def test_parked_live_owner_reduces_capacity_and_retries_deduplicate(tmp_path):
    from hermes_cli.worker_compute_capacity import occupied_owners, owner_slice

    owner = ('board-a', 't_one')
    name = owner_slice(*owner)
    tmp_path = tmp_path / 'cgroups'
    tmp_path.mkdir()
    group = tmp_path / name
    group.mkdir()
    (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    # No running row: the parked card's descendant still reserves a slot.
    assert occupied_owners(tmp_path, {name: owner}, set()) == {owner}
    # A resumed attempt does not reserve twice.
    assert occupied_owners(tmp_path, {name: owner}, {owner}) == {owner}
    (group / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
    assert occupied_owners(tmp_path, {name: owner}, set()) == set()


def test_unknown_populated_scope_reserves_and_failed_sample_refuses(tmp_path):
    from hermes_cli.worker_compute_capacity import occupied_owners

    tmp_path = tmp_path / 'cgroups'
    tmp_path.mkdir()
    group = tmp_path / 'legacy.scope'
    group.mkdir()
    (group / 'cgroup.events').write_text('populated 1\n')
    assert occupied_owners(tmp_path, {}, set()) == {('unknown-cgroup', 'legacy.scope')}
    (group / 'cgroup.events').write_text('frozen 0\n')
    with pytest.raises(ValueError, match='populated'):
        occupied_owners(tmp_path, {}, set())
