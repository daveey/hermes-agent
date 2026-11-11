"""Value-free diagnostics for slow writer transactions; never affect settlement."""
from __future__ import annotations

import logging
import os
import re
import sys
from types import FunctionType, MethodType

logger = logging.getLogger("hermes_state")


def log_write_duration(held_s: float, stage: str, callback: object) -> None:
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
        # Exact built-in types avoid invoking arbitrary callable properties/reprs.
        if type(callback) is MethodType:
            callback = callback.__func__
        callback_name = "opaque"
        if type(callback) is FunctionType:
            callback_name = re.sub(r"[^a-zA-Z0-9_.<>]", "_", callback.__code__.co_qualname)[:160]
        logger.warning(
            "state.db write lock held %.1fs (pid %d) stage=%s callback=%s callsites=%s",
            held_s, os.getpid(), stage, callback_name, " <- ".join(sites),
        )
    except Exception:  # health: allow BLE001,S110 -- diagnostic handlers must never change commit settlement or cause replay
        # No recursive logging here: the failed handler may be the cause.
        return
