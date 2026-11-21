"""Who else holds a chat: this host's gateway running a turn Hermes started, or a backend being replaced. [daveey]

On the hub, 2026-11-21, the gateway's own Kanban close-out / notice turns held three Desktop chats (39be4c 06:02Z,
8d8534 05:13Z, a30b 05:14Z). The user typed into each one; the Desktop backend's ``prompt.submit`` was refused
``SESSION_NOT_OWNED`` with ``auto_take_over`` (the newest turn was automated), the Desktop took the chat over, and
the gateway's turn died with ``session_turn_lease_lost`` mid model call. The holder was no other interactive window:
it was the gateway serving this home, whose FIFO already queues a typed send behind a running turn (RT-37,
``owner_forward``). So such a send is queued there instead (``methods_prompt._queue_behind_background_turn``), and
an explicit Stop takes the chat over, which ends that turn (``methods_session_interrupt``). A holder this module
cannot prove is the gateway (another Desktop backend, a CLI, an unreadable registry) keeps today's takeover.

:func:`retiring_owner_reason` answers the other question a waker asks when it is refused "has another live owner":
is that owner a backend about to restart (its core out of date, superseded)? Then the refusal is transient.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


def _holder_pid(holder: Any) -> Optional[int]:
    """The pid of a ``pid=<n>:...`` durable turn-lease holder, else None."""
    head = str(holder or "").split(":", 1)[0]
    if not head.startswith("pid="):
        return None
    digits = head[4:]
    return int(digits) if digits.isdigit() else None


def holder_pids(key: str, foreign_owner: Optional[dict], registry: Iterable[dict], me: int) -> set[int]:
    """Every OTHER process holding chat ``key``: the durable turn-lease writer and the registry leases. An entry
    whose pid cannot be read counts as pid 0, which no gateway has, so it keeps the takeover."""
    pids: set[int] = set()
    if foreign_owner:
        pid = _holder_pid(foreign_owner.get("holder"))
        pids.add(pid if pid is not None else 0)
    for entry in registry or ():
        if str(entry.get("session_id") or "") != key:
            continue
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid != me:
            pids.add(pid)
    return pids


def gateway_background_holder(session: dict, *, foreign_owner: Callable[[dict], Optional[dict]],
                              registry: Optional[Callable[[], list]] = None,
                              gateway_pid: Optional[Callable[[], Optional[int]]] = None) -> Optional[int]:
    """The live gateway's pid when it is the ONLY other holder of this chat, else None.

    The caller has already established that the chat's newest turn is one Hermes started (``_automation_holder_kind``).
    Every read failure answers None: an unprovable holder keeps the existing takeover behaviour."""
    key = str(session.get("session_key") or "")
    if not key:
        return None
    try:
        owner = foreign_owner(session)
        if registry is None:
            from hermes_cli.active_sessions import active_session_registry_snapshot
            entries = active_session_registry_snapshot(session.get("profile_home"))
        else:
            entries = registry()
        pids = holder_pids(key, owner, entries, os.getpid())
        if not pids:
            return None
        if gateway_pid is None:
            from hermes_cli.gateway_multiplex_served import live_default_gateway_pid
            live = live_default_gateway_pid()
        else:
            live = gateway_pid()
    except Exception:  # health: allow BLE001 -- an unprovable holder keeps the takeover; logged with traceback
        logger.warning("could not tell who holds chat %s", key, exc_info=True)
        return None
    if live is None or pids != {int(live)}:
        return None
    return int(live)


def retiring_owner_reason(session: dict, *, foreign_owner: Callable[[dict], Optional[dict]],
                          registry: Optional[Callable[[], list]] = None,
                          reason_of: Optional[Callable[[int], Optional[str]]] = None) -> Optional[str]:
    """Why every OTHER holder of this chat is a backend being replaced (``host_loop_lease.retiring_reason``: code
    skew, superseded), else None. One holder that is not retiring (or cannot be read) is a real owner: None.

    A wake refused by such a holder is transient: the backend restarts within seconds and lets the chat go
    (:func:`wake_past_retiring_owner`). Never raises."""
    key = str(session.get("session_key") or "")
    if not key:
        return None
    try:
        owner = foreign_owner(session)
        if registry is None:
            from hermes_cli.active_sessions import active_session_registry_snapshot
            entries = active_session_registry_snapshot(session.get("profile_home"))
        else:
            entries = registry()
        pids = holder_pids(key, owner, entries, os.getpid())
        if reason_of is None:
            from hermes_cli.host_loop_lease import retiring_reason as reason_of
        reasons = [reason_of(pid) if pid > 0 else None for pid in sorted(pids)]
    except Exception:  # health: allow BLE001 -- an unreadable owner is a real owner; logged with traceback
        logger.warning("could not tell whether chat %s is held by a retiring backend", key, exc_info=True)
        return None
    if not reasons or any(r is None for r in reasons):
        return None
    return "; ".join(sorted(set(reasons)))


# A wake refused by a backend that is being replaced retries this long, this often: the backend restarts within
# seconds and lets the chat go, where "refused" sent the waker (chat-closeout) to its long backoff.
RETIRING_OWNER_WAIT_S = 15.0
RETIRING_OWNER_POLL_S = 1.0


def wake_past_retiring_owner(sid: str, session: dict, label: str, key: str, *, claim_turn: Callable[[dict], bool],
                             release_turn: Callable[[dict], Any], claim_slot: Callable[..., Any],
                             foreign_owner: Callable[[dict], Optional[dict]],
                             sleep: Callable[[float], None] = time.sleep,
                             monotonic: Callable[[], float] = time.monotonic) -> Optional[str]:
    """``session_notifications._wake_session_turn`` after its slot claim was refused (its notice claim released):
    None once the slot is claimed (the notice claim is held again, as on the ordinary path), else the verdict.
    Every other holder retiring (:func:`retiring_owner_reason`): re-claim each ``RETIRING_OWNER_POLL_S`` for
    ``RETIRING_OWNER_WAIT_S``, then ``"busy"`` (transient: retry soon). The slot claim stays the only way in, so the
    restarting backend and this one never write at once. Any other owner: ``"refused"``, as before."""
    reason = retiring_owner_reason(session, foreign_owner=foreign_owner)
    if reason is None:
        logger.info("%s: %s has another live owner; not waking it here", label, key)
        return "refused"
    logger.info("%s: %s is held by a backend being replaced (%s); retrying for up to %.0fs",
                label, key, reason, RETIRING_OWNER_WAIT_S)
    deadline = monotonic() + RETIRING_OWNER_WAIT_S
    while monotonic() < deadline:
        sleep(RETIRING_OWNER_POLL_S)
        if session.get("_finalized") or not claim_turn(session):
            return "busy"
        if claim_slot(sid, session) is None:
            logger.info("%s: %s came free from the retiring backend", label, key)
            return None
        release_turn(session)
    logger.info("%s: %s is still held by the retiring backend; busy (retry soon)", label, key)
    return "busy"
