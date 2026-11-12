"""Bound the sum of Kanban run scopes, not just each individual run.

MemoryHigh starts reclaim at 70% of RAM; MemoryMax reserves at least 20%
for the gateway/control plane. All boards/profiles in this user manager share
one pool. The gateway itself is deliberately not moved into the pool.
"""
from functools import lru_cache
import subprocess


@lru_cache(maxsize=1)
def configure_pool():
    """Set runtime properties; only cache success, never launch unbounded on failure."""
    try:
        subprocess.run(
            ['systemctl', '--user', 'set-property', '--runtime', 'hermes-kanban.slice',
             'MemoryHigh=70%', 'MemoryMax=80%'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        from tools.process_registry import RestartSafeScopeUnavailable
        raise RestartSafeScopeUnavailable(f'Kanban aggregate memory budget unavailable: {exc}') from exc


def bounded_scope(argv, *, task_id=None, board=None):
    """Add the bounded pool, and the opt-in worker's stable per-card CPU slice."""
    from hermes_cli.worker_compute_capacity import configure_card, worker_enabled

    enabled = worker_enabled()
    if not argv or argv[0].rsplit('/', 1)[-1] != 'systemd-run' or '--scope' not in argv:
        if enabled:
            raise ValueError('Worker compute policy requires a systemd scope')
        return argv
    configure_pool()
    at = argv.index('--') if '--' in argv else argv.index('--scope') + 1
    if not enabled:
        return [*argv[:at], '--slice=hermes-kanban.slice', *argv[at:]]
    if not task_id or not board or '--' not in argv:
        raise ValueError('Worker compute scope requires board and task identity')
    name = configure_card(board, task_id)
    return [*argv[:at], f'--slice={name}', '--', 'nice', '-n', '15', *argv[at + 1:]]
