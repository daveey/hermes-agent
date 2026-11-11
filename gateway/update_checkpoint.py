"""Negotiated instant-update checkpoint for managed cards and explicitly cut cron runs.

Ordinary idle pauses remain unchanged. Unknown reservations/work fail closed;
this is not a forced-stop interface. The request binds to one live sync episode.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from hermes_constants import get_default_hermes_root, get_hermes_home
from hermes_cli import admission_fence
from gateway.cron_pause import preserve_cron_slots
from gateway.status import _pid_exists
from gateway.session_runtime_workers import kanban_managed_count
from gateway.run_shutdown_restart_wait import CORE_UPDATE_KANBAN_WAIT_S, CORE_UPDATE_KANBAN_STOP_LEASH_S

CHECKPOINT_PAUSE = True
_REQUEST_WAIT_S = 5.0
CHECKPOINT_DRAIN_S = CORE_UPDATE_KANBAN_WAIT_S + CORE_UPDATE_KANBAN_STOP_LEASH_S + 15.0


def checkpoint_intent(home):
    """Return the bounded, live unfenced episode, or None (never infer authority from an env flag)."""
    try:
        data = json.loads((Path(home) / "core-update/intent").read_text(encoding="utf-8-sig"))
        now = time.time()
        if (not isinstance(data, dict) or data.get("kind") != "unfenced"
                or data.get("checkpoint") is not True or type(data.get("pid")) is not int
                or data["pid"] <= 0 or not isinstance(data.get("nonce"), str) or not data["nonce"]
                or not 0 <= now - float(data["created_at"]) < 180
                or not now < float(data["expires_at"]) <= float(data["created_at"]) + 180):
            return None
        if not _pid_exists(data["pid"]):
            return None
        root = get_default_hermes_root(home=home)
        if not admission_fence.is_held(Path(root) / "core-update"):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def known_cron_reservations():
    """Exact published run reservations, never all files owned by the gateway PID.

    A tick or another admission in this same process can still be unpublished.
    Read outside the event-loop callback; cron registers under this lock.
    """
    from cron.scheduler import _running_lock, _running_job_ids, _running_reservations
    if not _running_lock.acquire(timeout=0.1):
        raise TimeoutError("cron reservation inventory busy")
    try:
        return {reservation.path.name for key, reservation in _running_reservations.items()
                if key in _running_job_ids and reservation.fd is not None}
    finally:
        _running_lock.release()


def checkpoint_inventory():
    home = get_hermes_home().resolve()
    root = get_default_hermes_root(home=home).resolve()
    return {"version": 1, "pid": os.getpid(), "home": str(home), "root": str(root),
            "reservation_ids": sorted(known_cron_reservations())}


def checkpoint_for_update(runner, loop, params):
    home = get_hermes_home()
    root = get_default_hermes_root(home=home)
    refused = {"pausing": False, "already_stopping": False, "busy": True, "pid": os.getpid()}
    intent = checkpoint_intent(home)
    intent_path = Path(home) / "core-update/intent"
    if intent is None or params.get("nonce") != intent["nonce"]:
        return refused
    # No admission lock is held while reading cron's lock (cron uses the reverse order).
    try:
        known = known_cron_reservations() if intent.get("cron_cut") is True else set()
    except (OSError, TimeoutError):
        return refused
    done = threading.Event()
    result = []
    deadline = time.monotonic() + _REQUEST_WAIT_S
    gate = threading.Lock()
    cancelled = False
    claimed = False
    pending = {"pausing": False, "already_stopping": True, "checkpoint": True,
               "nonce": intent["nonce"], "pid": os.getpid(), "drain_timeout": CHECKPOINT_DRAIN_S}

    def request():
        nonlocal claimed
        try:
            if time.monotonic() >= deadline or checkpoint_intent(home) != intent:
                return
            # No waiting on the event loop. A lock race or an unpublished reservation retries.
            state = admission_fence.state(root, details=True, timeout=0)
            if not state["held"] or not set(state["reservation_ids"]).issubset(known):
                return
            from gateway.run_active_work_verb import _delegation_units
            cron = runner._active_cron_job_count() if intent.get("cron_cut") is True else 0
            if (_delegation_units(time.time())
                    or runner._active_work_count() != kanban_managed_count(runner) + cron):
                return
            preserve_cron_slots(runner.config)
            # Atomically claim permission versus the handler's timeout. Never hold
            # this lock across runner code: even a claimed request must return bounded.
            with gate:
                if cancelled or time.monotonic() >= deadline:
                    return
                claimed = True
            # The admitted path, not a fresh lookup: a withdrawal during preserve must still cancel.
            accepted = runner.request_restart(detached=False, via_service=True, update_intent=intent_path)
            result.append({"pausing": bool(accepted), "already_stopping": not accepted,
                           "checkpoint": True, "nonce": intent["nonce"], "pid": os.getpid(), "drain_timeout": CHECKPOINT_DRAIN_S})
        except (OSError, TimeoutError, ValueError, TypeError, KeyError):
            return
        finally:
            done.set()

    loop.call_soon_threadsafe(request)
    done.wait(timeout=_REQUEST_WAIT_S)
    with gate:
        cancelled = True
        # Claimed-but-incomplete is unknown, not refusal: the updater must settle
        # the recorded identity, never assume an ACK authorizes tree mutation.
        return result[0] if result else (pending if claimed else refused)
