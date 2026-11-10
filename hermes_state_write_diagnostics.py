"""Value-free diagnostics for slow writer transactions; never affect settlement."""
from __future__ import annotations

import logging
import os
import re
import sys

logger = logging.getLogger("hermes_state")


def log_write_duration(held_s: float, stage: str) -> None:
    """Attribute a completed/failed hold to code, including generic SQL wrappers.

    Never inspect SQL, parameters, locals, callable reprs, or exception text. The
    bounded stack survives wrapper calls and identifies their semantic caller.
    This runs only for slow holds, after commit/rollback; logging must not make a
    committed operation look failed (and invite a caller to retry it).
    """
    try:
        frame = sys._getframe(1)
        sites = []
        try:
            for _ in range(4):
                if frame is None:
                    break
                code = frame.f_code
                name = re.sub(r"[^a-zA-Z0-9_.<>]", "_", code.co_qualname)[:160]
                filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", os.path.basename(code.co_filename))[:80]
                sites.append(f"{filename}:{frame.f_lineno}:{name}")
                frame = frame.f_back
        finally:
            del frame
        logger.warning(
            "state.db write lock held %.1fs (pid %d) stage=%s callsites=%s",
            held_s, os.getpid(), stage, " <- ".join(sites),
        )
    except Exception:  # health: allow BLE001,S110 -- diagnostic handlers must never change commit settlement or cause replay
        # No recursive logging here: the failed handler may be the cause.
        return
