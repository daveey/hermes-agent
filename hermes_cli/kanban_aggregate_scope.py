"""Bound the sum of Kanban run scopes, not just each individual run.

MemoryHigh starts reclaim at 70% of RAM; MemoryMax reserves at least 20%
for the gateway/control plane. All boards/profiles in this user manager share
one pool. The gateway itself is deliberately not moved into the pool.
"""
from functools import lru_cache
import logging
import socket
import subprocess
import time


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def configure_pool():
    """Set runtime properties; only cache success, never launch unbounded on failure."""
    for attempt, delay in enumerate((1, 3, None), start=1):
        try:
            subprocess.run(
                ['systemctl', '--user', 'set-property', '--runtime', 'hermes-kanban.slice',
                 'MemoryHigh=70%', 'MemoryMax=80%'], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
            return
        except (OSError, subprocess.SubprocessError) as exc:
            stderr = getattr(exc, 'stderr', None) or ''
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', errors='replace')
            message = (
                f'Kanban aggregate memory budget unavailable on {socket.gethostname()}: '
                f'returncode={getattr(exc, "returncode", None)}; stderr={stderr[:500]}; {exc}'
            )
            logger.warning('%s (attempt %s/3)', message, attempt)
            if delay is None:
                from tools.process_registry import RestartSafeScopeUnavailable
                raise RestartSafeScopeUnavailable(message) from exc
            time.sleep(delay)


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
