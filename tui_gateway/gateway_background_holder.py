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
import threading
import time
from typing import Any, Callable, Iterable, NamedTuple, Optional

logger = logging.getLogger(__name__)


def _holder_pid(holder: Any) -> Optional[int]:
    """The pid of a ``pid=<n>:...`` durable turn-lease holder, else None."""
    head = str(holder or "").split(":", 1)[0]
    if not head.startswith("pid="):
        return None
    digits = head[4:]
    return int(digits) if digits.isdigit() else None


def _durable_holder_pid(holder: Any) -> int:
    """The durable writer's pid when it names a process in THIS pid namespace (``hermes_state_pidns``), else 0:
    the same number in another namespace (a container, a VM's shared store) is not that process here."""
    pid = _holder_pid(holder)
    if pid is None:
        return 0
    from hermes_state_pidns import holder_pid_checkable
    return pid if holder_pid_checkable(str(holder)) else 0


def _registry_pid(entry: dict) -> int:
    """A registry lease's pid when it is this host's live process with the recorded start time, else 0 (a lease
    from another host, a dead process, or a reused pid is not that process)."""
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        return 0
    from hermes_cli.active_sessions import _pid_liveness, owner_host
    host = entry.get("host")
    if pid <= 0 or (host not in (None, "") and str(host) != owner_host()):
        return 0
    return pid if _pid_liveness(pid, entry.get("process_start_time")) is True else 0


def holder_pids(key: str, foreign_owner: Optional[dict], registry: Iterable[dict], me: int, *,
                durable_pid: Callable[[Any], int] = _durable_holder_pid,
                registry_pid: Callable[[dict], int] = _registry_pid) -> set[int]:
    """Every OTHER process holding chat ``key``: the durable turn-lease writer and the registry leases. A holder
    whose identity cannot be proved here counts as pid 0, which no gateway has, so it keeps the takeover."""
    pids: set[int] = set()
    if foreign_owner:
        pids.add(durable_pid(foreign_owner.get("holder")))
    for entry in registry or ():
        if str(entry.get("session_id") or "") != key:
            continue
        pid = registry_pid(entry)
        if pid != me:
            pids.add(pid)
    return pids


class DialedGateway(NamedTuple):
    """The gateway ``owner_forward`` dials for a chat: its endpoint and its verified live pid."""
    pid: int
    endpoint: Any


def dialed_gateway(session: dict) -> Optional[DialedGateway]:
    """The ready gateway serving the chat's profile home (``owner_forward.discover_owner``, which follows a named
    profile to the multiplexer that serves it) and the pid of the process answering on its control home, else
    None. Discovery only: never starts one."""
    from pathlib import Path
    from gateway.status import live_gateway_pid_for_home
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from tui_gateway import server
    home = Path(session.get("profile_home") or server._hermes_home).resolve()
    discovery = discover_gateway_endpoint(home, timeout=5)
    if discovery.state != "ready" or discovery.endpoint is None:
        return None
    pid = live_gateway_pid_for_home(Path(discovery.endpoint.control_home or home))
    return DialedGateway(int(pid), discovery.endpoint) if pid else None


def gateway_background_holder(session: dict, *, foreign_owner: Callable[[dict], Optional[dict]],
                              registry: Optional[Callable[[], list]] = None,
                              dialed: Optional[Callable[[dict], Optional[DialedGateway]]] = None,
                              holders: Callable[..., set] = holder_pids) -> Optional[DialedGateway]:
    """The gateway a forward would dial, when that very process is the ONLY other holder of this chat, else None.

    The caller has already established that the chat's newest turn is one Hermes started (``_automation_holder_kind``).
    A holder that is another process, unprovable here, or a gateway other than the one the forward dials (another
    profile's, a restarted one) keeps the takeover. Every read failure answers None."""
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
        pids = holders(key, owner, entries, os.getpid())
        if not pids or 0 in pids or len(pids) != 1:
            return None
        gateway = (dialed or dialed_gateway)(session)
    except Exception:  # health: allow BLE001 -- an unprovable holder keeps the takeover; logged with traceback
        logger.warning("could not tell who holds chat %s", key, exc_info=True)
        return None
    if gateway is None or pids != {gateway.pid}:
        return None
    return gateway


def automatic_owner_wait(session: dict, refusal, *, foreign_owner: Callable[[dict], Optional[dict]]) -> Optional[str]:
    """Presentation only: automatic work retries behind its ready gateway, never takes custody.

    A durable gateway lease and a gateway queue are two representations of the same busy owner.
    Unproved/foreign writers and typed sends retain their existing actionable refusal.
    """
    if not session.get("_synthesized_turn"):
        return None
    from tui_gateway.owner_forward import FORWARDABLE_LEDGER_STATES
    status = getattr(refusal, "ledger_status", None)
    if status in FORWARDABLE_LEDGER_STATES:
        return f"gateway queue holds {status} work"
    if (getattr(refusal, "reason", None) != "SESSION_NOT_OWNED"
            or getattr(refusal, "ownership_cause", None) != "lease_foreign"):
        return None
    if gateway_background_holder(session, foreign_owner=foreign_owner) is not None:
        return "local gateway holds the durable turn lease"
    return None


def retiring_owner_reason(session: dict, *, foreign_owner: Callable[[dict], Optional[dict]],
                          registry: Optional[Callable[[], list]] = None,
                          reason_of: Optional[Callable[[int], Optional[str]]] = None,
                          holders: Callable[..., set] = holder_pids) -> Optional[str]:
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
        pids = holders(key, owner, entries, os.getpid())
        if reason_of is None:
            from hermes_cli.host_loop_lease import retiring_reason as reason_of
        reasons = [reason_of(pid) if pid > 0 else None for pid in sorted(pids)]
    except Exception:  # health: allow BLE001 -- an unreadable owner is a real owner; logged with traceback
        logger.warning("could not tell whether chat %s is held by a retiring backend", key, exc_info=True)
        return None
    if not reasons or any(r is None for r in reasons):
        return None
    return "; ".join(sorted(set(reasons)))


# A wake refused by a backend that is being replaced is retried this soon: the backend restarts within seconds and
# lets the chat go, where "refused" sent the waker (chat-closeout) to its long backoff. The wake answers "busy" at
# once (it never sleeps inside a caller's wake slot) and :func:`retiring_retry_at` gives the short retry time.
RETIRING_OWNER_RETRY_S = 15.0
# A recorded retry older than this is from an earlier wake (its caller never asked): ignored, never reused.
RETIRING_RECORD_MAX_AGE_S = 30.0
_retiring_busy: dict[str, tuple[float, float]] = {}  # chat key -> (retry_at, written_at) of its last refusal
_retiring_mu = threading.Lock()


def wake_past_retiring_owner(sid: str, session: dict, label: str, key: str, *,
                             foreign_owner: Callable[[dict], Optional[dict]],
                             now: Callable[[], float] = time.time) -> str:
    """``session_notifications._wake_session_turn`` after its slot claim was refused (its notice claim released):
    ``"busy"`` (transient, retry in ``RETIRING_OWNER_RETRY_S``, see :func:`retiring_retry_at`) when every other
    holder is a backend being replaced (:func:`retiring_owner_reason`), else ``"refused"`` as before. Nothing is
    claimed here, so the restarting backend and this one never write at once."""
    reason = retiring_owner_reason(session, foreign_owner=foreign_owner)
    if reason is None:
        logger.info("%s: %s has another live owner; not waking it here", label, key)
        return "refused"
    written = now()
    with _retiring_mu:
        _retiring_busy[str(key)] = (written + RETIRING_OWNER_RETRY_S, written)
    logger.info("%s: %s is held by a backend being replaced (%s); busy, retry in %.0fs",
                label, key, reason, RETIRING_OWNER_RETRY_S)
    return "busy"


def retiring_retry_at(key: str, *, now: Callable[[], float] = time.time) -> Optional[float]:
    """The short retry time of ``key``'s latest busy-because-retiring wake, consumed (one answer per wake); None
    when there is none or it was written more than ``RETIRING_RECORD_MAX_AGE_S`` ago (another wake's)."""
    with _retiring_mu:
        record = _retiring_busy.pop(str(key), None)
    if record is None or now() - record[1] > RETIRING_RECORD_MAX_AGE_S:
        return None
    return record[0]
