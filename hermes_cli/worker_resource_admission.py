"""Opt-in worker gate, independent of hub overflow and placement policy."""
from __future__ import annotations


import json
import logging
import os
from pathlib import Path
import tempfile
import time

from hermes_cli import worker_compute_capacity as capacity

# Short hysteresis writers should not cause prelease/final-gate churn. Keep the
# wait bounded; final admission also checks its whole-lock budget after sampling.
PRESSURE_LOCK_TIMEOUT = 0.25
# One log line per distinct failure per window: refusal() runs per card per tick.
UNKNOWN_LOG_INTERVAL = 600.0
_unknown_logged: dict[str, float] = {}


def _log_unknown(exc: BaseException) -> None:
    """Name the failing check (e.g. the missing registered board) in gateway.log."""
    detail = f'{type(exc).__name__}: {exc}'
    now = time.monotonic()
    if detail in _unknown_logged and now - _unknown_logged[detail] < UNKNOWN_LOG_INTERVAL:
        return
    _unknown_logged[detail] = now
    logging.getLogger(__name__).warning(
        'worker_resources_unknown: %s (board inventory: %s)', detail,
        capacity.policy_root() / 'worker-resources' / 'boards')


def _lock_pressure(lock):
    import fcntl
    deadline = time.monotonic() + PRESSURE_LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.01, remaining))


def _pressure(directory: Path) -> bool:
    """Serialize hysteresis updates, including pre-lease callers across boards.

    The observation is taken inside the lock, never replayed from a reporter.
    Corrupt state or a lock still busy after a bounded wait refuses rather than
    erasing a held brake.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'pressure.lock').open('a') as lock:
        _lock_pressure(lock)
        path = directory / 'pressure.json'
        try:
            state = json.loads(path.read_text(encoding='utf-8-sig'))
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


def pool_occupancy(pool: Path, owners, active, *, legacy_resolver=None):
    """A not-yet-started slice is empty, but a missing hierarchy is unknown.

    Inspect the manager first after ENOENT, then retry the slice inventory;
    never turn a disappearing child or unreadable events file into free slots.
    """
    try:
        pool.stat()
    except FileNotFoundError:
        events = dict(line.split() for line in (pool.parent.parent / 'cgroup.events').read_text(encoding='utf-8-sig').splitlines())
        if events.get('populated') not in {'0', '1'}:
            raise ValueError('invalid user manager observation')
        try:
            pool.stat()
        except FileNotFoundError:
            return set(active)
    return capacity.occupied_owners(pool, owners, active, legacy_resolver=legacy_resolver)


def occupied_count_for_host(echo=None, *, claimant=None) -> int | None:
    """One census for admission and Board reports; never mutate pressure state."""
    from hermes_cli import machine_metrics
    from hermes_cli.worker_account_boards import paths
    active_owners = set()
    active = machine_metrics.active_card_runs(exclude=echo, owner_output=active_owners, board_paths=paths())
    if active is None:
        return None
    owners = {}
    owner_dir = capacity.policy_root() / 'worker-resources' / 'owners'
    if owner_dir.exists():
        for path in owner_dir.iterdir():
            if path.suffix != '.json':
                continue
            owner = json.loads(path.read_text(encoding='utf-8-sig'))
            if (not isinstance(owner, list) or len(owner) != 2
                    or not all(isinstance(value, str) and value for value in owner)
                    or capacity.owner_slice(*owner) != path.stem):
                raise ValueError('invalid worker owner map')
            owners[path.stem] = tuple(owner)
    from hermes_cli import worker_legacy_scopes
    occupied = pool_occupancy(capacity.pool_path(), owners, active_owners,
                              legacy_resolver=worker_legacy_scopes.lookup)
    # A prospective resume in a populated slice does not take a second slot.
    if claimant is not None:
        occupied.add(claimant)
    # Distinct installations can contain copied IDs. Preserve their row count.
    return len(occupied) + max(0, active - len(active_owners))


def refusal(echo=None, *, enabled=None, claimant=None) -> str | None:
    """Pre-lease and final-lock check. Unknown observations fail closed.

    Every answer of an enabled worker feeds the host-wide unknown-streak alarm
    (``worker_resources_unknown_alarm``)."""
    from hermes_cli import worker_resources_unknown_alarm as alarm
    try:
        if not (capacity.worker_enabled() if enabled is None else enabled):
            return None
    except Exception as exc:  # health: allow BLE001 -- any unreadable policy/controller must refuse claims
        _log_unknown(exc)
        alarm.observe(True, exc)
        return 'worker_resources_unknown'
    try:
        answer = _refusal(echo, claimant=claimant)
    except Exception as exc:  # health: allow BLE001 -- any unreadable policy/controller must refuse claims
        _log_unknown(exc)
        alarm.observe(True, exc)
        return 'worker_resources_unknown'
    alarm.observe(answer == 'worker_resources_unknown')
    return answer


def _refusal(echo, *, claimant):
    directory = capacity.policy_root() / 'worker-resources'
    if _pressure(directory):
        return 'worker_load_pressure'
    occupied_count = occupied_count_for_host(echo, claimant=claimant)
    from hermes_cli.kanban_admission import _root_max_in_progress
    ok, cap = _root_max_in_progress(root=capacity.policy_root())
    if occupied_count is None or not ok or cap is None:
        return 'worker_resources_unknown'
    if occupied_count > cap if claimant is not None else occupied_count >= cap:
        return 'worker_capacity_full'
    return None
