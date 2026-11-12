"""Host capacity primitives for opt-in, cgroup-contained worker execution.

The caller owns the host admission lock and durable board/task-to-slice map.
A parked task is still occupied while its slice contains any descendants.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def pressure_held(held: bool, load: float, cores: float) -> bool:
    """Load/core hysteresis; invalid observations must refuse rather than release."""
    if not math.isfinite(load) or load < 0 or not math.isfinite(cores) or cores <= 0:
        raise ValueError('invalid load/core observation')
    return load / cores > 1.0 if held else load / cores >= 1.5

Owner = tuple[str, str]


def owner_slice(board: str, task: str) -> str:
    """Stable across retries and profiles; hex cannot create systemd nesting."""
    digest = hashlib.sha256(json.dumps([board, task]).encode()).hexdigest()
    return f"hermes-kanban-{digest}.slice"


def occupied_owners(root: Path, owners: dict[str, Owner], active: set[Owner]) -> set[Owner]:
    """Union active cards with populated child units, including unknown legacy jobs.

    Read errors propagate: an incomplete inventory cannot prove free capacity.
    cgroup.events populated includes descendants, unlike cgroup.procs.
    """
    occupied = set(active)
    for child in root.iterdir():
        if not child.is_dir():
            continue
        events = dict(line.split() for line in (child / 'cgroup.events').read_text(encoding='utf-8').splitlines())
        populated = events.get('populated')
        if populated not in {'0', '1'}:
            raise ValueError(f'invalid populated observation for {child.name}')
        if populated == '1':
            occupied.add(owners.get(child.name, ('unknown-cgroup', child.name)))
    return occupied


def worker_enabled() -> bool:
    """Read installation policy, never a card's environment or profile override."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly

    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / 'config.yaml')
    except FileNotFoundError:
        return False
    raw = ((doc or {}).get('kanban') or {}).get('worker_resources', {})
    if not isinstance(raw, dict) or type(raw.get('enabled', False)) is not bool:
        raise ValueError('invalid worker_resources policy')
    return raw.get('enabled', False)


def configure_card(board: str, task: str) -> str:
    """Verify the root backstop, configure/read back CPU quotas, retain ownership.

    No success cache: cpuset changes and changed controller settings must be seen.
    The existing aggregate memory pool is configured by the caller.
    """
    import os
    import subprocess
    from hermes_constants import get_default_hermes_root
    from tools.process_registry import RestartSafeScopeUnavailable, systemd_user_bus_env

    env = systemd_user_bus_env()
    if env is None:
        raise RestartSafeScopeUnavailable('Worker compute budget requires the user bus')
    cores = len(os.sched_getaffinity(0))
    base = Path('/sys/fs/cgroup/user.slice') / f'user-{os.getuid()}.slice'
    _verify_quota(base, cores * .875)
    name = owner_slice(board, task)
    manager = base / f'user@{os.getuid()}.service'
    pool = manager / 'hermes.slice' / 'hermes-kanban.slice'
    for unit, quota, path in [('hermes-kanban.slice', cores * .75, pool), (name, 2, pool / name)]:
        for arguments in [('set-property', '--runtime', unit, f'CPUQuota={quota * 100:g}%', 'CPUWeight=10'),
                          ('start', unit)]:
            subprocess.run(['systemctl', '--user', *arguments], env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        _verify_quota(path, quota)
    directory = Path(get_default_hermes_root()) / 'worker-resources' / 'owners'
    directory.mkdir(parents=True, exist_ok=True)
    # Immutable one-file-per-owner: concurrent retries write identical bytes.
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump([board, task], handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, directory / f'{name}.json')
    finally:
        temporary.unlink(missing_ok=True)
    return name


def _verify_quota(path: Path, maximum: float) -> None:
    raw, period = (path / 'cpu.max').read_text(encoding='utf-8').split()
    if raw == 'max' or int(period) <= 0 or int(raw) <= 0 or int(raw) / int(period) > maximum:
        raise ValueError(f'Worker CPU quota is missing or too wide: {path.name}')
