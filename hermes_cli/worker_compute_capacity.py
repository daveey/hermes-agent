"""Host capacity primitives for opt-in, cgroup-contained worker execution.

The caller owns the host admission lock and durable board/task-to-slice map.
A parked task is still occupied while its slice contains any descendants.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

CGROUP_ROOT = Path('/sys/fs/cgroup')


def effective_cores() -> float:
    """Host cpuset/quota, not the caller's two-core card budget.

    Worker mode requires the host unified hierarchy; enrollment rejects a
    container-private hierarchy. Re-read on every admission/launch.
    """
    import os
    cpus = set()
    for span in (CGROUP_ROOT / 'cpuset.cpus.effective').read_text(encoding='utf-8-sig').strip().split(','):
        bounds = span.split('-')
        low, high = int(bounds[0]), int(bounds[-1])
        if len(bounds) > 2 or low < 0 or high < low or high > 1048576:
            raise ValueError('invalid host cpuset')
        part = set(range(low, high + 1))
        if part & cpus:
            raise ValueError('overlapping host cpuset')
        cpus.update(part)
    cores = float(len(cpus & os.sched_getaffinity(0)))
    quota_file = CGROUP_ROOT / 'cpu.max'
    # The real cgroup-v2 root has no cpu.max; a delegated mount may have one.
    if quota_file.exists():
        raw, period = quota_file.read_text(encoding='utf-8-sig').split()
        if int(period) <= 0:
            raise ValueError('invalid host CPU period')
        if raw != 'max':
            cores = min(cores, int(raw) / int(period))
    if cores <= 0:
        raise ValueError('empty host CPU budget')
    return cores


def owner_for_db(database, task: str) -> tuple[str, str]:
    """Stable local execution identity, independent of caller board labels."""
    if not isinstance(database, (str, Path)) or not database or not Path(database).is_absolute():
        raise ValueError('Worker identity requires an absolute database path')
    if not isinstance(task, str) or not task:
        raise ValueError('Worker identity requires a task')
    return str(Path(database).resolve()), task


def owner_for_connection(conn, task: str) -> tuple[str, str]:
    from hermes_cli.kanban_db_connect import _main_db_file
    return owner_for_db(_main_db_file(conn), task)


def managed_target(policy, target: Path | None) -> bool:
    """Validate the actual owner-launched interpreter's destination before spawn."""
    if not policy or not getattr(policy, 'kanban_json', None) or not worker_enabled():
        return False
    context = json.loads(policy.kanban_json)
    board, task = owner_for_db(context.get('db'), context.get('task_id'))
    name = configure_card(board, task)
    if target is None or target.parent.name != name:
        raise ValueError('Managed worker is outside its card CPU slice')
    return True


def lower_priority(pid: int) -> None:
    """The managed child is born from the gateway, not the nice'd submitter."""
    import os
    os.setpriority(os.PRIO_PROCESS, pid, max(15, os.getpriority(os.PRIO_PROCESS, pid)))


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


def occupied_owners(root: Path, owners: dict[str, Owner], active: set[Owner], *,
                    legacy_resolver=None) -> set[Owner]:
    """Union active cards with populated child units, including unknown legacy jobs.

    Read errors propagate: an incomplete inventory cannot prove free capacity.
    cgroup.events populated includes descendants, unlike cgroup.procs.
    """
    import stat
    occupied = set(active)
    for child in root.iterdir():
        before = child.lstat()
        if not stat.S_ISDIR(before.st_mode):
            continue
        owner = owners.get(child.name)
        # Resolve against this sample's identity, never an earlier name census.
        if owner is None and legacy_resolver is not None:
            owner = legacy_resolver(root, child.name)
        events = dict(line.split() for line in (child / 'cgroup.events').read_text(encoding='utf-8-sig').splitlines())
        try:
            after = child.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f'worker scope changed during census: {child.name}') from exc
        if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
            raise ValueError(f'worker scope changed during census: {child.name}')
        populated = events.get('populated')
        if populated not in {'0', '1'}:
            raise ValueError(f'invalid populated observation for {child.name}')
        if populated == '1':
            occupied.add(owner if owner is not None else ('unknown-cgroup', child.name))
    return occupied


def pool_path() -> Path:
    import os
    uid = os.getuid()  # windows-footgun: ok (Linux cgroup-v2 only)
    return CGROUP_ROOT / 'user.slice' / f'user-{uid}.slice' / f'user@{uid}.service' / 'hermes.slice' / 'hermes-kanban.slice'


def policy_root() -> Path:
    """Host policy is account-owned, independent of HOME and HERMES_HOME.

    Worker enrollment uses this fixed Linux account installation. Custom profile
    roots remain valid for session data, but cannot disable host CPU policy.
    The one exception is a test harness's temp root (:func:`_test_policy_override`).
    """
    override = _test_policy_override()
    if override is not None:
        return override
    from hermes_constants import _account_home
    return _account_home() / '.hermes'


def _test_policy_override(environ=None) -> Path | None:
    """``HERMES_TEST_POLICY_ROOT``, honoured only as an absolute path under a temp root.

    A non-pytest suite (state's unittest ``run-state-suite-local``) rebinds HOME,
    which never moved :func:`policy_root`: a test's core child that claimed a card
    registered its throwaway board in the REAL account inventory, and once the
    test deleted it every claim on the host deferred ``worker_resources_unknown``
    (worker-1, 2026-11-13, t_01a1d5bf526e1344). The suite points the policy at
    its shard dir instead. Temp roots only (:func:`_override_temp_roots`, deny by
    default): a durable path is never honoured, whatever TMPDIR says, so the
    override cannot disable host CPU policy by accident.
    """
    import os
    raw = (os.environ if environ is None else environ).get('HERMES_TEST_POLICY_ROOT', '')
    if not raw or not os.path.isabs(raw):
        return None
    path = Path(os.path.realpath(raw))
    for real in _override_temp_roots():
        if path != real and path.is_relative_to(real):
            return path
    return None


# no-tmp: ok — recognises a harness's temp root; nothing is written here
SYSTEM_TEMP_ROOTS: tuple[str, ...] = ('/tmp', '/var/tmp', '/dev/shm')
# macOS per-user temp: TMPDIR=/var/folders/<xx>/<id>/T (realpath under /private/var/folders).
MACOS_USER_TEMP_PARENTS: tuple[str, ...] = ('/var/folders', '/private/var/folders')


def _override_temp_roots() -> list[Path]:
    """Roots a policy override may live under: the system temp roots, plus TMPDIR only when it is known-ephemeral.

    An honoured empty root turns :func:`worker_enabled` off, so ``refusal()`` lets every claim through. Deny by
    default: TMPDIR joins only when it sits under a system temp root, is a macOS per-user ``/var/folders/*/*/T``
    dir, or is inside the account's pruned ``.hermes/cache/scratch``; any other TMPDIR (``/``, a home, the
    account, ``/srv/data``) leaves only the fixed system roots. A TMPDIR that contains the passwd account or
    HOME never joins either. Judged against the passwd account, never the override itself.
    """
    import os
    import tempfile
    from hermes_constants import _account_home
    roots = [Path(os.path.realpath(root)) for root in SYSTEM_TEMP_ROOTS]
    account = Path(os.path.realpath(_account_home()))
    home = Path(os.path.realpath(os.path.expanduser('~')))
    tmp = Path(os.path.realpath(tempfile.gettempdir()))
    if account.is_relative_to(tmp) or home.is_relative_to(tmp):
        return roots
    macos = [Path(os.path.realpath(parent)) for parent in MACOS_USER_TEMP_PARENTS]
    # The scratch dir is resolved like tmp (the mini's ~/.hermes and cache/scratch are both symlinks,
    # t_01a1d974a89f6ff6), and last: realpath reads the account home, so a TMPDIR the system or macOS
    # rules already admit never touches it.
    ephemeral = (any(tmp.is_relative_to(root) for root in roots)
                 or any(tmp.is_relative_to(parent) and tmp.relative_to(parent).parts[2:3] == ('T',)
                        for parent in macos)
                 or tmp.is_relative_to(Path(os.path.realpath(account / '.hermes' / 'cache' / 'scratch'))))
    if ephemeral:
        roots.append(tmp)
    return roots


def worker_enabled() -> bool:
    """Read installation policy, never a card's environment or profile override.

    The only environment that moves it is a test harness's ``HERMES_TEST_POLICY_ROOT`` under a temp root
    (:func:`_test_policy_override`).
    """
    from hermes_cli.config_backend import read_config_doc_readonly

    try:
        doc = read_config_doc_readonly(policy_root() / 'config.yaml')
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
    from tools.process_registry import RestartSafeScopeUnavailable, systemd_user_bus_env

    env = systemd_user_bus_env()
    if env is None:
        raise RestartSafeScopeUnavailable('Worker compute budget requires the user bus')
    cores = effective_cores()
    base = CGROUP_ROOT / 'user.slice' / f'user-{os.getuid()}.slice'  # windows-footgun: ok (Linux cgroup-v2 only)
    _verify_quota(base, cores * .875)
    name = owner_slice(board, task)
    manager = base / f'user@{os.getuid()}.service'  # windows-footgun: ok (Linux cgroup-v2 only)
    pool = manager / 'hermes.slice' / 'hermes-kanban.slice'
    for unit, quota, path in [('hermes-kanban.slice', cores * .75, pool), (name, 2, pool / name)]:
        for arguments in [('set-property', '--runtime', unit, f'CPUQuota={quota * 100:g}%', 'CPUWeight=10'),
                          ('start', unit)]:
            subprocess.run(['systemctl', '--user', *arguments], env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        _verify_quota(path, quota)
        if not 1 <= int((path / 'cpu.weight').read_text(encoding='utf-8-sig')) <= 10:
            raise ValueError(f'Worker CPU weight is missing or too high: {unit}')
    directory = policy_root() / 'worker-resources' / 'owners'
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
    raw, period = (path / 'cpu.max').read_text(encoding='utf-8-sig').split()
    if raw == 'max' or int(period) <= 0 or int(raw) <= 0 or int(raw) / int(period) > maximum:
        raise ValueError(f'Worker CPU quota is missing or too wide: {path.name}')
