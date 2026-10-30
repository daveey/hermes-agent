"""Gateway lifecycle-signal helpers split out of ``gateway.run`` (SIGUSR1 restart, repeat stop signals)."""

from __future__ import annotations

import logging

# Same logger as gateway.run: these lines belong to the gateway's lifecycle log.
logger = logging.getLogger("gateway.run")


def _start_gateway_make_restart_signal_handler(runner):
    """Build the SIGUSR1 handler: log what the signal means, then the drain-aware service restart."""
    def restart_signal_handler():
        # systemd's `reload` verb (ExecReload=kill -USR1) lands here too; say so, because operators
        # expect `reload` to mean an in-process config reload, not a drain-and-relaunch (#117267).
        logger.info(
            "SIGUSR1 received (systemctl reload / hermes gateway restart): performing a graceful "
            "gateway restart — drain active turns, exit, supervisor relaunches. Not an in-process "
            "config reload.")
        runner.request_restart(detached=False, via_service=True)
    return restart_signal_handler


def repeat_stop_signal(runner, counter: list, received_signal) -> bool:
    """[daveey] Count one stop signal; True when a shutdown already started (the caller then does nothing).

    One launchd stop can reach the gateway several times within a few ms (the job's group signal plus each
    forwarder: signed wrapper, core-update launcher, stderr_timestamp). Each used to start its own shutdown
    pass (forensics snapshot + detached diagnostic, deadline arm, ``runner.stop()``); repeats are now only
    logged with their number, and the count rides on the exit-signal record (``stop_signals``).
    """
    counter[0] += 1
    runner._stop_signal_count = counter[0]
    if counter[0] == 1:
        return False
    if isinstance(unplanned := getattr(runner, "_unplanned_signal", None), dict):
        unplanned["stop_signals"] = counter[0]
    logger.info("Received %s again during shutdown (stop signal #%d) — already stopping, ignored",
                getattr(received_signal, "name", None) or "planned-stop marker", counter[0])
    return True
