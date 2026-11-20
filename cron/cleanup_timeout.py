"""Bounded cron cleanup with drain custody retained by the actual worker."""
from __future__ import annotations

import threading

from agent.memory_provider import spawn_context_thread


def run_cleanup(cleanup, *, job_id, label, timeout, logger) -> bool:
    from hermes_cli.worker_drain_fence import admit, lease_scope

    # Acquire BEFORE thread construction; inherited parent authority may close
    # as soon as this function times out. The independent descriptor cannot.
    lease = admit('cron.cleanup')

    def owned_cleanup():
        try:
            with lease_scope(lease):
                cleanup()
        finally:
            if lease is not None:
                lease.close()

    if timeout <= 0:
        try:
            owned_cleanup()
            return True
        except (Exception, KeyboardInterrupt) as exc:  # health: allow BLE001 -- cleanup failures are logged
            logger.debug("Job '%s': %s failed: %s", job_id, label, exc)
            return False

    done = threading.Event()
    error: list[BaseException] = []

    def runner():
        try:
            owned_cleanup()
        except BaseException as exc:
            logger.debug("Job '%s': %s cleanup worker failed", job_id, label, exc_info=True)
            error.append(exc)
        finally:
            done.set()

    try:
        # A daemon thread may outlive the timeout, never the retained lease.
        worker = spawn_context_thread(runner, name=f'cron-cleanup-{job_id}', daemon=True)
        worker.start()
    except BaseException:
        # start() can be interrupted after native creation but before returning.
        # Never release custody while still claiming this daemon fully covered.
        from hermes_cli.worker_drain_capability import revoke
        revoke('cron cleanup thread start outcome uncertain')
        if lease is not None:
            lease.close()
        raise
    if not done.wait(timeout):
        logger.error("Job '%s': %s exceeded %.1fs; abandoning cleanup so future runs remain dispatchable",
                     job_id, label, timeout)
        return False
    if error:
        logger.debug("Job '%s': %s failed: %s", job_id, label, error[0])
        return False
    return True
