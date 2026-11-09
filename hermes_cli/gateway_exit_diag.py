"""``logs/gateway-exit-diag.log``: one JSON line per way ``hermes gateway run`` starts and ends.

Each record carries a ``level`` so log shippers grade it by intent rather than by its words: a
supervised restart (exit 75 back to launchd/systemd after a SIGUSR1 drain, ``hermes update`` or
``/restart``) is routine, a non-zero return a warning, and a crash or failing exit code an error.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

# An unplanned stop signal (launchctl bootout, kickstart -k, systemctl stop) is logged as
# ``gateway.exit_signal`` (info, with signal + parent), a start refused because another gateway owns
# this home/host as ``gateway.start_refused`` (info, with reason + owner pid/liveness); a bare
# non-zero return is a warning; only an escaped exception or a failing SystemExit is an error.
# [daveey] gateway.launchd_budget_missing: a darwin --external-supervisor gateway could not read launchd's live
# ExitTimeOut and assumed the gui clamp (gateway-cc28b19bca); the coach files it as a defect.
_TAG_LEVELS = {"asyncio.run.exception": "error", "gateway.exit_nonzero": "warning",
               "gateway.launchd_budget_missing": "warning"}


def _record_level(tag: str, extra: dict[str, object]) -> str:
    if tag in _TAG_LEVELS:
        return _TAG_LEVELS[tag]
    if tag == "asyncio.run.SystemExit":
        from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
        return "info" if extra.get("code") in (None, 0, GATEWAY_SERVICE_RESTART_EXIT_CODE) else "error"
    return "info"


def nonzero_exit_record() -> tuple[str, dict]:
    """Tag + fields for a False ``start_gateway()`` return. An unplanned stop signal (bootout,
    kickstart -k, systemctl stop) exits 1 on purpose so the service manager revives us, and a start
    refused because another gateway owns this home/host exits 1 too: neither is a crash, so each
    names its cause. Any other False is ``gateway.exit_nonzero`` with the ``cause`` (and ``exit_reason``)
    its exit site noted via ``note_exit_cause()``, or ``cause: "unrecorded"`` when it noted none."""
    from gateway.run_exit_reason import last_start_refusal
    from gateway.run_shutdown import last_signal_exit
    if (sig := last_signal_exit()) is not None:
        return "gateway.exit_signal", sig
    if (refusal := last_start_refusal()) is not None:
        # [daveey] A refusal by a LIVE owner under a service manager means the supervisor restarted into a gateway
        # still draining (gateway-cc28b19bca); the coach self-heal collector keys on this flag.
        if "--external-supervisor" in sys.argv[1:]:
            refusal = {**refusal, "external_supervisor": True}
        return "gateway.start_refused", refusal
    # [daveey] Any other False exit carries the cause its exit site noted; a site that noted none
    # says so ("unrecorded"), which the coach files as a missing note, not as an old crash.
    from gateway.run_exit_reason import last_exit_cause
    return "gateway.exit_nonzero", dict(last_exit_cause() or {"cause": "unrecorded"})


def make_exit_diag():
    """``_exit_diag(tag, **extra)`` recorder writing ``logs/gateway-exit-diag.log`` — captures every way
    ``asyncio.run()`` can return, for chasing silent Windows gateway deaths. HERMES_GATEWAY_EXIT_DIAG=0 opts out."""

    def _exit_diag(tag: str, **extra: object) -> None:
        if os.environ.get("HERMES_GATEWAY_EXIT_DIAG", "1") != "1":
            return
        try:
            from hermes_constants import get_hermes_home as _ghh
            log_dir = _ghh() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            line = {
                "ts": datetime.now(UTC).isoformat(), "tag": tag, "level": _record_level(tag, extra),
                "pid": os.getpid(), "python": sys.version.split()[0], "platform": sys.platform, **extra,
            }
            with open(log_dir / "gateway-exit-diag.log", "a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        except Exception:  # health: allow BLE001 -- never let the diagnostic itself crash the gateway
            logging.getLogger(__name__).debug("gateway exit diag write failed for %s", tag, exc_info=True)

    return _exit_diag
