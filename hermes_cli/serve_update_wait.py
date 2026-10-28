"""[daveey] ``hermes serve`` waits out a core update before it imports the agent stack.

A backend that boots while ``hermes update`` is between its drain and its checkout imports the old
tree and keeps serving it until something retires it (``web_server_skew_exit``, which only fires
between turns). David-MBP 2026-10-28: the mac-mini Desktop respawned its SSH backend (pid 75803) at
00:51:23.55, after hermes-sync had taken the admission hold (00:51:19) but before ``hermes update``
had started and written its marker (00:51:25). The checkout moved at 00:51:44, the backend had
finished its imports by 00:51:31, and it served pre-update code until 01:32.

So the startup gate watches both signals:

* a live update (the ``.hermes-update-in-progress`` marker or a held checkout lock, the same
  ``update_lock`` reads the always-on dashboard and Desktop's spawn gate use). Waited out up to
  :data:`UPDATE_WAIT_S` (the always-on dashboard's 600 s policy). A Desktop client that stops waiting
  for READY kills this process and its own spawn gate then parks on the marker, so a long wait never
  wedges a client.
* the hermes-sync admission hold (``<root>/core-update/holds/hermes-sync``), which is set seconds
  before ``hermes update`` starts. A hold alone does not prove an update is coming (a drain window
  holds it while turns finish), so it is waited out for at most :data:`HOLD_WAIT_S`; if a marker
  appears meanwhile, the update bound applies.

Both bounds fail open: after them the backend starts anyway (a stale marker must not keep it down),
and the code-skew watchdog still retires it once the checkout has moved. Our own update tree is
exempt (an updater that spawns ``serve`` must not wait on itself). ``HERMES_SERVE_UPDATE_WAIT_S=0``
turns the gate off.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

UPDATE_WAIT_S = 600.0
HOLD_WAIT_S = 60.0
POLL_S = 1.0


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def update_live() -> bool:
    """A live ``hermes update`` owns this install, and it is not the update that spawned us."""
    try:
        from hermes_cli import update_lock as ul

        holder = ul.read_live_update()
        if holder is not None and holder.pid:
            return not (holder.pid == os.getpid() or ul._is_ancestor_pid(holder.pid))
        if holder is None and not ul.checkout_lock_held():
            return False
        # A held checkout lock (with or without a dead marker): ours if we inherited it.
        return ul._inherited_lock_fd(ul.checkout_lock_path()) is None
    except Exception:
        return False  # the marker read fails open everywhere else too (update_lock doc)


def admission_hold_present(root: Optional[Path] = None) -> bool:
    """hermes-sync's admission hold is set: an update may start any second."""
    try:
        from hermes_cli.admission_fence import is_held

        if root is None:
            from hermes_constants import get_default_hermes_root

            root = get_default_hermes_root()
        return is_held(Path(root) / "core-update")
    except Exception:
        return False  # a hint, not a fence: an unreadable hold must not delay every start


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def wait_for_update_clear(*, update_probe: Optional[Callable[[], bool]] = None,
                          hold_probe: Optional[Callable[[], bool]] = None,
                          update_timeout_s: Optional[float] = None,
                          hold_timeout_s: Optional[float] = None,
                          sleep: Callable[[float], None] = time.sleep,
                          now: Callable[[], float] = time.monotonic,
                          log: Optional[Callable[[str], None]] = None) -> str:
    """Block until no update is live and no admission hold is set, within the bounds.

    Returns ``"clear"`` (nothing to wait for), ``"cleared"`` (waited, now clear),
    ``"update-timeout"`` or ``"hold-timeout"`` (gave up; the caller starts anyway).
    """
    if update_timeout_s is None:
        update_timeout_s = _env_seconds("HERMES_SERVE_UPDATE_WAIT_S", UPDATE_WAIT_S)
    if hold_timeout_s is None:
        hold_timeout_s = min(update_timeout_s, _env_seconds("HERMES_SERVE_HOLD_WAIT_S", HOLD_WAIT_S))
    emit = log or _stderr
    update_probe = update_probe or update_live
    hold_probe = hold_probe or admission_hold_present
    if update_timeout_s <= 0:
        return "clear"
    start = now()
    waiting_on = ""
    seen_update = False
    while True:
        live = update_probe()
        seen_update = seen_update or live
        # Once an update has run and released its marker, the checkout is the new one: a hold
        # hermes-sync has not dropped yet is no reason to keep waiting.
        held = not live and not seen_update and hold_probe()
        elapsed = now() - start
        if not live and not held:
            if waiting_on:
                emit(f"serve: core update clear after {elapsed:.0f}s; starting on the current checkout")
                return "cleared"
            return "clear"
        reason = "update" if live else "hold"
        if live and elapsed >= update_timeout_s:
            emit(f"serve: core update still in progress after {elapsed:.0f}s; starting anyway")
            return "update-timeout"
        if held and elapsed >= hold_timeout_s:
            emit(f"serve: update admission hold still set after {elapsed:.0f}s and no update started; "
                "starting anyway")
            return "hold-timeout"
        if reason != waiting_on:
            what = ("a core update is in progress" if live
                    else "the update admission hold is set (an update is about to start)")
            emit(f"serve: {what}; waiting before loading the agent so this backend runs the new code "
                f"(bound {update_timeout_s if live else hold_timeout_s:.0f}s)")
            waiting_on = reason
        sleep(_env_seconds("HERMES_SERVE_UPDATE_POLL_S", POLL_S) or POLL_S)
