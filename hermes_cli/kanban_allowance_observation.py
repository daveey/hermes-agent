"""Bounded Board allowance diagnostics for decision recovery, never claim authority. [daveey]

Read only outside SQLite transactions. An observation is usable only for the same
Board and override, within its original request deadline. Missing/malformed/failed
reads are unknown, not exhausted and never permission to claim. The real lease
endpoint remains the sole spending authority on every claim.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from hermes_cli import kanban_spend_allowance as allowance

READ_SECONDS = 5.0


@dataclass(frozen=True)
class Observation:
    board: str
    expected: dict
    remaining: int
    deadline: float


def _remaining(reply, board, task, want):
    if not isinstance(reply, dict) or reply.get("board") != board:
        return None
    data = reply.get("allowance")
    if not isinstance(data, dict):
        return None
    coordinates = {"runs_basis": want["basis"], "task": task, "review_uid": want["review_uid"],
                   "budget_event_uid": want["budget_event_uid"], "state": "active"}
    if any(data.get(key) != value for key, value in coordinates.items()):
        return None
    if any(not isinstance(data.get(key), str) or not data[key] for key in ("home", "episode")):
        return None
    runs, used, remaining = (data.get(key) for key in ("runs", "used", "remaining"))
    if (any(type(value) is not int for value in (runs, used, remaining)) or runs != allowance.BOARD_RUNS
            or not 0 <= used <= runs or remaining != runs - used):
        return None
    return remaining


def read(conn, task_id, *, deadline: float | None = None) -> Observation | None:
    """One authenticated bounded read; refuse transaction callers before opening transport."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    if conn.in_transaction:
        return None
    want, state = allowance.expected(conn, task_id), sync.sync_state(conn)
    if want is None or not state:
        return None
    now = time.monotonic()
    deadline = min(now + READ_SECONDS, deadline) if deadline is not None else now + READ_SECONDS
    if deadline <= now:
        return None
    hub = None
    try:
        hub = client.Hub(client.load_identity(), deadline=deadline)
        remaining = _remaining(hub.reviewer_allowance(state["board_id"], task_id), state["board_id"], task_id, want)
        if remaining is None or time.monotonic() >= deadline:
            return None
        return Observation(state["board_id"], want, remaining, deadline)
    except (client.SyncUnavailable, client.HubError, OSError, TimeoutError, ValueError):
        return None  # unknown diagnostic; the ordinary exact-debit claim guard still refuses
    finally:
        if hub is not None:
            hub.close()


def current(conn, task_id, observed: Observation | None) -> bool:
    """Recheck local identity and freshness under the caller's write transaction; no I/O."""
    from hermes_cli import kanban_db_sync as sync
    if observed is None or time.monotonic() >= observed.deadline:
        return False
    state = sync.sync_state(conn)
    return bool(state and state["board_id"] == observed.board and allowance.expected(conn, task_id) == observed.expected)


@dataclass(frozen=True)
class Held:
    """The Board still holds a private allowance although the card's newest local budget is a generic one."""
    board: str
    budget: object  # the newest local override row at read time (None: no override)
    review_uid: str
    state: str  # active | invalidated
    deadline: float


def _budget(conn, task_id):
    row = allowance._newest(conn, task_id)
    return tuple(row) if row is not None else None


def _held(reply, board, task):
    """None (unknown), ``{}`` (the Board holds no allowance) or its validated active/invalidated allowance."""
    if not isinstance(reply, dict) or reply.get("board") != board or "allowance" not in reply:
        return None
    data = reply["allowance"]
    if data is None:
        return {}
    if (not isinstance(data, dict) or data.get("runs_basis") != allowance.BASIS or data.get("task") != task
            or data.get("state") not in ("active", "invalidated")
            or any(not isinstance(data.get(key), str) or not data[key] for key in ("review_uid", "budget_event_uid"))):
        return None
    return data


def board_state(conn, task_id, *, deadline: float | None = None):
    """What the Board itself holds for the card, whatever the local budget says: None when unknown, ``{}`` when
    it holds none, else its allowance. One authenticated bounded read, never under a transaction, never authority."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    state = None if conn.in_transaction else sync.sync_state(conn)
    if not state or sync.board_mode(conn) != "primary":
        return None
    now = time.monotonic()
    deadline = min(now + READ_SECONDS, deadline) if deadline is not None else now + READ_SECONDS
    if deadline <= now:
        return None
    hub = None
    try:
        hub = client.Hub(client.load_identity(), deadline=deadline)
        seen = _held(hub.reviewer_allowance(state["board_id"], task_id), state["board_id"], task_id)
        return None if time.monotonic() >= deadline else seen
    except (client.SyncUnavailable, client.HubError, OSError, TimeoutError, ValueError):
        return None
    finally:
        if hub is not None:
            hub.close()


def stuck(conn, task_id, *, deadline: float | None = None) -> Held | None:
    """A decided-but-stuck card: a generic budget is newest locally (cumulative accounting, so the claim states
    no allowance) while the Board still holds one and refuses every such grant. Unknown reads are not stuck."""
    from hermes_cli import kanban_db_sync as sync
    if conn.in_transaction or allowance.state(conn, task_id) is not None or not allowance.history(conn, task_id):
        return None
    now = time.monotonic()
    deadline = min(now + READ_SECONDS, deadline) if deadline is not None else now + READ_SECONDS
    seen = board_state(conn, task_id, deadline=deadline)
    if not seen:
        return None
    return Held(sync.sync_state(conn)["board_id"], _budget(conn, task_id), seen["review_uid"], seen["state"],
                deadline)


def still_stuck(conn, task_id, held: Held | None) -> bool:
    """Recheck under the caller's write transaction; no I/O. A newer budget of any kind invalidates the reading."""
    from hermes_cli import kanban_db_sync as sync
    if held is None or time.monotonic() >= held.deadline:
        return False
    state = sync.sync_state(conn)
    return bool(state and state["board_id"] == held.board and allowance.state(conn, task_id) is None
                and _budget(conn, task_id) == held.budget)
