"""Persist the cron pause before a gateway can exit between ticker cycles."""

import os
import threading


def pause_for_update(runner, loop) -> dict:
    """Control-socket executor thread: record slots before marshaling the drain onto the loop."""
    preserve_cron_slots(runner.config)
    try:
        from hermes_cli.gateway import _get_restart_drain_timeout
        drain = float(_get_restart_drain_timeout())
    except (OSError, ValueError, TypeError):
        drain = 30.0
    accepted_box: list[bool] = []
    done = threading.Event()

    def request() -> None:
        try:
            accepted_box.append(runner.request_restart(detached=False, via_service=True))
        finally:
            done.set()

    loop.call_soon_threadsafe(request)
    done.wait(timeout=5.0)
    accepted = bool(accepted_box and accepted_box[0])
    return {"pausing": accepted, "already_stopping": not accepted,
            "pid": os.getpid(), "drain_timeout": drain}


def preserve_cron_slots(config) -> None:
    from cron.planned_pause import begin
    from cron.scheduler_provider import _profile_cron_scope
    from gateway.run import _cron_profile_gate, _cron_tick_profile_homes
    import logging

    for name, home in _cron_tick_profile_homes(config):
        if not home.is_dir() or not _cron_profile_gate(name, home):
            continue
        try:
            with _profile_cron_scope(home):
                begin()
        except (OSError, TimeoutError):
            logging.getLogger("gateway.run").warning("Could not preserve cron slots for %s", home, exc_info=True)
