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


@pytest.mark.parametrize('raw, honoured', [
    ('/dev/shm/state-suite-local-x/shard-5/account-policy/.hermes', True),
    ('/tmp/run/account-policy/.hermes', True),
    ('/dev/shm', False),  # the temp root itself is no harness dir
    ('relative/.hermes', False),
    ('/home/ubuntu/.hermes', False),  # never somewhere durable: a worker's real policy stays put
    ('/srv/policy/.hermes', False),
    ('', False),
])
def test_test_policy_root_is_honoured_only_under_a_temp_root(raw, honoured):
    """state's run-state-suite-local (unittest, no pytest) rebinds HOME, which never moved policy_root: a test's
    core child registered its board in worker-1's real inventory and held every claim once the board was deleted
    (2026-11-13, t_01a1d5bf526e1344). The suite points HERMES_TEST_POLICY_ROOT at its shard dir instead."""
    import os
    from pathlib import Path
    from hermes_cli import worker_compute_capacity as capacity
    got = capacity._test_policy_override({'HERMES_TEST_POLICY_ROOT': raw})
    assert got == (Path(os.path.realpath(raw)) if honoured else None)


def test_policy_root_follows_the_override_in_a_child(tmp_path):
    """The unpatched policy_root (a fresh interpreter, no conftest) answers the shard dir, not the account."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path('/dev/shm') if os.access('/dev/shm', os.W_OK) else Path('/tmp')
    want = root / f'policy-override-{os.getpid()}' / '.hermes'
    env = {**os.environ, 'HERMES_TEST_POLICY_ROOT': str(want)}
    code = 'from hermes_cli import worker_compute_capacity as c; print(c.policy_root())'
    out = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parents[2]), check=True).stdout.strip()
    assert out == os.path.realpath(want)
    env.pop('HERMES_TEST_POLICY_ROOT')
    out = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parents[2]), check=True).stdout.strip()
    assert out != os.path.realpath(want) and out.endswith('/.hermes')
