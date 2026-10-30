"""Hand a supervised home back to its supervisor (self-heal defect gateway-e2cd786685) [daveey].

The mini supervises ``~/.bb-hermes`` with a KeepAlive LaunchAgent under its own label
(``com.daveey.babybear.hermes``). Twice an unsupervised (manual) gateway took that home and locked the
job out in an exit-75 restart loop: first a client spawned one in the job's restart gap, then
``hermes update`` relaunched that manual gateway manual again. These helpers are the two places the
CLI now gives the home back instead:

* :func:`supervised_attach_exit_code` -- a supervised ``gateway run`` that would just ATTACH to a live
  gateway launched from its own home records the refusal and asks a manual owner to drain.
* :func:`custom_job_serves` -- ``hermes update`` stops a manual gateway of a home such a job serves
  instead of relaunching it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from gateway.restart import GATEWAY_FATAL_CONFIG_EXIT_CODE, GATEWAY_SERVICE_RESTART_EXIT_CODE

logger = logging.getLogger("hermes_cli.gateway")


def _host_decision_exit_code(decision) -> int:
    """Exit code for a host-attach verdict a supervisor may be watching.

    ``GATEWAY_FATAL_CONFIG_EXIT_CODE`` (78) is the PERMANENT refusal: systemd parks the unit on it
    (``RestartPreventExitStatus``), the s6 finish script maps it to 125, launchd maps it to a
    deliberate stop. That is right for a config-derived refusal and wrong for a runtime one — "some
    other process serves me right now" ends the moment that process goes away, and parking the unit
    on it strands the profile until a human notices. Transient verdicts therefore use
    ``GATEWAY_SERVICE_RESTART_EXIT_CODE`` (75, EX_TEMPFAIL), which every supervisor we generate
    already retries: systemd has ``RestartForceExitStatus=75`` with ``RestartSec=5``, the s6 finish
    script passes it through, and launchd relaunches a non-78 failure. Exit 0 would NOT do: s6
    parks a clean exit too.
    """
    if getattr(decision, "transient", False):
        return GATEWAY_SERVICE_RESTART_EXIT_CODE
    return GATEWAY_FATAL_CONFIG_EXIT_CODE


def supervised_attach_exit_code(decision) -> int:
    """[daveey] A supervised start that ATTACHES to a live gateway launched from THIS home is a refusal.

    ``hermes update`` (run by hermes-sync) relaunched an unsupervised gateway for the home that the
    mini's KeepAlive job supervises. Once that gateway published its host record, every KeepAlive retry
    stopped in the host-attach guard ("nothing to start", exit 75) before ``start_gateway`` ran, so
    neither the duplicate guard's handback (``run_bootstrap._hand_home_back_to_supervisor``) nor any
    exit-diag row ever happened, and the job looped for hours with no signal. Record the refusal in
    ``gateway-exit-diag.log`` (the coach's wedge detector reads it) and ask a MANUAL owner to drain.
    A profile unit attaching to a host multiplexer launched from another home is a real attach and is
    left alone. Returns the supervisor exit code either way.
    """
    owner = getattr(decision, "owner", None)
    if owner is not None:
        try:
            from hermes_cli.config import get_hermes_home
            if Path(owner.home).expanduser().resolve() == Path(get_hermes_home()).expanduser().resolve():
                from gateway.run_bootstrap import _hand_home_back_to_supervisor
                from gateway.run_exit_reason import note_start_refused
                from hermes_cli.gateway_exit_diag import make_exit_diag, nonzero_exit_record
                note_start_refused("already_running", owner_pid=owner.pid, detail="host_attach")
                _hand_home_back_to_supervisor(owner.pid)
                tag, extra = nonzero_exit_record()
                make_exit_diag()(tag, **extra)
        except Exception:  # health: allow BLE001 -- recording/handback is best-effort; the attach exit stands
            logger.debug("Supervised attach refusal record failed", exc_info=True)
    return _host_decision_exit_code(decision)


def custom_job_serves(profile: str) -> bool:
    """[daveey] A KeepAlive LaunchAgent under any label serves this profile's home (macOS).

    gateway-e2cd786685 (second occurrence): a client-spawned MANUAL gateway held the mini's
    ``~/.bb-hermes`` when hermes-sync ran ``hermes update``; the updater armed a detached watcher that
    relaunched it manual again, so ``com.daveey.babybear.hermes`` stayed locked out (exit 75 loop).
    Stopping it and letting the KeepAlive job respawn hands the home back instead. Any probe failure
    answers False so the update keeps its old relaunch.
    """
    import time
    try:
        from hermes_cli.gateway_runtime_service import custom_launchd_supervisor_for_home
        from hermes_cli.profiles import get_profile_dir
        service = custom_launchd_supervisor_for_home(get_profile_dir(profile), deadline=time.monotonic() + 10)
    except Exception:  # health: allow BLE001 -- unknown supervisor state keeps the old relaunch path
        logger.debug("custom launchd supervisor probe failed for %s", profile, exc_info=True)
        return False
    if service is None:
        return False
    print(f"  → {profile}: its home is served by a KeepAlive launchd job "
          f"({service.start_argv[-1]}); stopping the manual gateway so that job takes the home back")
    return True
