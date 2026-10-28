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
# ``gateway.exit_signal`` (info, with signal + parent); a bare non-zero return is a warning; only an
# escaped exception or a failing SystemExit is an error.
_TAG_LEVELS = {"asyncio.run.exception": "error", "gateway.exit_nonzero": "warning"}


def _record_level(tag: str, extra: dict[str, object]) -> str:
    if tag in _TAG_LEVELS:
        return _TAG_LEVELS[tag]
    if tag == "asyncio.run.SystemExit":
        from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
        return "info" if extra.get("code") in (None, 0, GATEWAY_SERVICE_RESTART_EXIT_CODE) else "error"
    return "info"


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
