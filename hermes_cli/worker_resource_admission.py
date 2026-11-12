"""Opt-in worker gate, independent of hub overflow and placement policy."""
from __future__ import annotations


import json
import os
from pathlib import Path
import tempfile

from hermes_cli import worker_compute_capacity as capacity


def _pressure(directory: Path) -> bool:
    """Serialize hysteresis updates, including pre-lease callers across boards.

    The observation is taken inside the lock, never replayed from a reporter.
    Corrupt state or a busy lock refuses rather than erasing a held brake.
    """
    import fcntl
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'pressure.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'pressure.json'
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            state = {'held': False}
        if not isinstance(state, dict) or type(state.get('held')) is not bool:
            raise ValueError('invalid worker pressure state')
        load, cores = os.getloadavg()[0], capacity.effective_cores()
        held = capacity.pressure_held(state['held'], load, cores)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({'held': held, 'load1': load, 'cores': cores}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return held


def pool_occupancy(pool: Path, owners, active):
    """A not-yet-started slice is empty, but a missing hierarchy is unknown.

    Inspect the manager first after ENOENT, then retry the slice inventory;
    never turn a disappearing child or unreadable events file into free slots.
    """
    try:
        pool.stat()
    except FileNotFoundError:
        events = dict(line.split() for line in (pool.parent.parent / 'cgroup.events').read_text(encoding='utf-8').splitlines())
        if events.get('populated') not in {'0', '1'}:
            raise ValueError('invalid user manager observation')
        try:
            pool.stat()
        except FileNotFoundError:
            return set(active)
    return capacity.occupied_owners(pool, owners, active)


def refusal(echo=None, *, enabled=None) -> str | None:
    """Pre-lease and final-lock check. Unknown observations fail closed."""
    try:
        if not (capacity.worker_enabled() if enabled is None else enabled):
            return None
        directory = capacity.policy_root() / 'worker-resources'
        if _pressure(directory):
            return 'worker_load_pressure'
        from hermes_cli import machine_metrics
        from hermes_cli.kanban_admission import _root_max_in_progress
        active_owners = set()
        from hermes_cli.worker_account_boards import paths
        active = machine_metrics.active_card_runs(exclude=echo, owner_output=active_owners, extra_paths=paths())
        ok, cap = _root_max_in_progress(root=capacity.policy_root())
        if active is None or not ok or cap is None:
            return 'worker_resources_unknown'
        owners = {}
        owner_dir = directory / 'owners'
        if owner_dir.exists():
            for path in owner_dir.iterdir():
                if path.suffix != '.json':
                    continue
                owner = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(owner, list) or len(owner) != 2
                        or not all(isinstance(value, str) and value for value in owner)
                        or capacity.owner_slice(*owner) != path.stem):
                    raise ValueError('invalid worker owner map')
                owners[path.stem] = tuple(owner)
        occupied = pool_occupancy(capacity.pool_path(), owners, active_owners)
        # Distinct installations can contain copied board/task IDs. Until a
        # cgroup supplies canonical custody, never collapse their running rows.
        occupied_count = len(occupied) + max(0, active - len(active_owners))
        if occupied_count >= cap:
            return 'worker_capacity_full'
        return None
    except Exception:  # health: allow BLE001 -- any unreadable policy/controller must refuse claims
        return 'worker_resources_unknown'
