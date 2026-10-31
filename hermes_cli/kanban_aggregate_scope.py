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


def bounded_scope(argv):
    """Add the shared bounded slice only to the systemd scope wrapper, never worker args."""
    if not argv or argv[0].rsplit('/', 1)[-1] != 'systemd-run' or '--scope' not in argv:
        return argv
    configure_pool()
    at = argv.index('--') if '--' in argv else argv.index('--scope') + 1  # systemd-run option space only
    return [*argv[:at], '--slice=hermes-kanban.slice', *argv[at:]]
